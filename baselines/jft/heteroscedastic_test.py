# coding=utf-8
# Copyright 2021 The Uncertainty Baselines Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the heteroscedastic ViT on JFT-300M model script."""
import os
import pathlib
import tempfile

from absl import flags
from absl import logging
from absl.testing import parameterized
import jax
import ml_collections
import tensorflow as tf
import tensorflow_datasets as tfds
import heteroscedastic  # local file import

flags.adopt_module_key_flags(heteroscedastic)
FLAGS = flags.FLAGS


def get_config(classifier, representation_size):
  """Config for training a patch-transformer on JFT."""
  config = ml_collections.ConfigDict()
  config.seed = 0

  # TODO(dusenberrymw): JFT + mocking is broken.
  # config.dataset = 'jft/entity:1.0.0'
  # config.val_split = 'test[:49511]'  # aka tiny_test/test[:5%] in task_adapt
  # config.train_split = 'train'  # task_adapt used train+validation so +64167
  # config.num_classes = 18291

  config.dataset = 'imagenet21k'
  config.val_split = 'full[:9]'
  config.train_split = 'full[30:60]'
  config.num_classes = 21843

  config.batch_size = 3
  config.prefetch_to_device = 1
  config.shuffle_buffer_size = 20

  config.total_steps = 3
  config.log_training_steps = config.total_steps
  config.log_eval_steps = config.total_steps
  config.checkpoint_steps = config.total_steps
  config.keep_checkpoint_steps = config.total_steps

  pp_common = '|value_range(-1, 1)'
  pp_common += f'|onehot({config.num_classes})'
  pp_common += '|keep("image", "labels")'
  # TODO(dusenberrymw): Mocking doesn't seem to encode into jpeg format.
  # config.pp_train = 'decode_jpeg_and_inception_crop(224)|flip_lr' + pp_common
  config.pp_train = 'decode|resize_small(256)|central_crop(224)' + pp_common
  config.pp_eval = 'decode|resize_small(256)|central_crop(224)' + pp_common

  config.init_head_bias = 1e-3

  # Model section
  config.model = ml_collections.ConfigDict()
  config.model.patches = ml_collections.ConfigDict()
  config.model.patches.size = [16, 16]
  config.model.hidden_size = 4
  config.model.transformer = ml_collections.ConfigDict()
  config.model.transformer.attention_dropout_rate = 0.
  config.model.transformer.dropout_rate = 0.
  config.model.transformer.mlp_dim = 3
  config.model.transformer.num_heads = 2
  config.model.transformer.num_layers = 1
  config.model.classifier = classifier
  config.model.representation_size = representation_size

  # Optimizer section
  config.optim_name = 'Adam'
  config.optim = ml_collections.ConfigDict()
  config.optim.weight_decay = 0.1
  config.optim.beta1 = 0.9
  config.optim.beta2 = 0.999
  config.weight_decay = None  # No explicit weight decay

  config.lr = ml_collections.ConfigDict()
  config.lr.base = 0.1
  config.lr.warmup_steps = 1
  config.lr.decay_type = 'linear'
  config.lr.linear_end = 1e-3

  # Few-shot eval section
  config.fewshot = ml_collections.ConfigDict()
  config.fewshot.representation_layer = 'pre_logits'
  config.fewshot.log_steps = config.total_steps
  config.fewshot.datasets = {
      'pets': ('oxford_iiit_pet', 'train', 'test'),
      'imagenet': ('imagenet2012_subset/10pct', 'train', 'validation'),
  }
  config.fewshot.pp_train = 'decode|resize(256)|central_crop(224)|value_range(-1,1)'
  config.fewshot.pp_eval = 'decode|resize(256)|central_crop(224)|value_range(-1,1)'
  config.fewshot.shots = [10]
  config.fewshot.l2_regs = [2.0**-6]
  config.fewshot.walk_first = ('imagenet', config.fewshot.shots[0])

  return config


class HeteroscedasticTest(parameterized.TestCase, tf.test.TestCase):

  @parameterized.parameters(
      ('token', 2, 15925.0, 14882.082899305555, 0.1899999976158142),
      ('token', None, 15594.578, 12385.49609375, 0.19999999552965164),
      ('gap', 2, 14589.023, 14325.224392361111, 0.16999999433755875),
      ('gap', None, 14479.479, 14084.083333333334, 0.25),
  )
  def test_heteroscedastic_script(self, classifier, representation_size,
                                  correct_train_loss, correct_val_loss,
                                  correct_fewshot_acc_sum):
    # Set flags.
    FLAGS.xm_runlocal = True
    FLAGS.config = get_config(
        classifier=classifier, representation_size=representation_size)
    FLAGS.output_dir = tempfile.mkdtemp(dir=self.get_temp_dir())

    # Go two directories up to the root of the UB directory.
    ub_root_dir = pathlib.Path(__file__).parents[2]
    data_dir = str(ub_root_dir) + '/.tfds/metadata'
    logging.info('data_dir contents: %s', os.listdir(data_dir))
    FLAGS.config.dataset_dir = data_dir

    # Check for any errors.
    with tfds.testing.mock_data(num_examples=100, data_dir=data_dir):
      train_loss, val_loss, fewshot_results = heteroscedastic.main(None)

    # Check for reproducibility.
    fewshot_acc_sum = sum(jax.tree_util.tree_flatten(fewshot_results)[0])
    logging.info('(train_loss, val_loss, fewshot_acc_sum) = %s, %s, %s',
                 train_loss, val_loss, fewshot_acc_sum)
    self.assertAllClose(train_loss, correct_train_loss)
    self.assertAllClose(val_loss, correct_val_loss)
    self.assertAllClose(fewshot_acc_sum, correct_fewshot_acc_sum)

    # Check for the ability to restart from a previous checkpoint (after
    # failure, etc.).
    FLAGS.output_dir = tempfile.mkdtemp(dir=self.get_temp_dir())
    # NOTE: Use this flag to simulate failing at a certain step.
    FLAGS.config.testing_failure_step = FLAGS.config.total_steps - 1
    with tfds.testing.mock_data(num_examples=100, data_dir=data_dir):
      heteroscedastic.main(None)

    # This should resume from the failed step.
    del FLAGS.config.testing_failure_step
    with tfds.testing.mock_data(num_examples=100, data_dir=data_dir):
      train_loss, val_loss, fewshot_results = heteroscedastic.main(None)

    fewshot_acc_sum = sum(jax.tree_util.tree_flatten(fewshot_results)[0])
    logging.info('(train_loss, val_loss, fewshot_acc_sum) = %s, %s, %s',
                 train_loss, val_loss, fewshot_acc_sum)
    self.assertAllClose(train_loss, correct_train_loss)
    self.assertAllClose(val_loss, correct_val_loss)
    self.assertAllClose(fewshot_acc_sum, correct_fewshot_acc_sum)

  @parameterized.parameters(
      ('token', 2, 5.3612347, 4.898042678833008, 0.09999999776482582),
      ('token', None, 5.631799, 5.07450728946262, 0.11999999359250069),
      ('gap', 2, 6.4580526, 6.147392484876844, 0.08999999985098839),
      ('gap', None, 6.4652596, 6.158123440212673, 0.08999999985098839),
  )
  def test_loading_pretrained_model(self, classifier, representation_size,
                                    correct_train_loss, correct_val_loss,
                                    correct_fewshot_acc_sum):
    # Set flags.
    FLAGS.xm_runlocal = True
    FLAGS.config = get_config(
        classifier=classifier, representation_size=representation_size)
    FLAGS.output_dir = tempfile.mkdtemp(dir=self.get_temp_dir())

    # Go two directories up to the root of the UB directory.
    ub_root_dir = pathlib.Path(__file__).parents[2]
    data_dir = str(ub_root_dir) + '/.tfds/metadata'
    logging.info('data_dir contents: %s', os.listdir(data_dir))
    FLAGS.config.dataset_dir = data_dir

    # Run to save a checkpoint, then use that as a pretrained model.
    FLAGS.output_dir = tempfile.mkdtemp(dir=self.get_temp_dir())
    with tfds.testing.mock_data(num_examples=100, data_dir=data_dir):
      heteroscedastic.main(None)

    previous_output_dir = FLAGS.output_dir
    FLAGS.output_dir = tempfile.mkdtemp(dir=self.get_temp_dir())
    FLAGS.config.model_init = os.path.join(previous_output_dir,
                                           'checkpoint.npz')
    FLAGS.config.model.representation_size = None
    FLAGS.config.dataset = 'cifar10'
    FLAGS.config.val_split = 'train[:9]'
    FLAGS.config.train_split = 'train[30:60]'
    FLAGS.config.num_classes = 10
    pp_common = '|value_range(-1, 1)'
    pp_common += f'|onehot({FLAGS.config.num_classes}, key="label", key_result="labels")'  # pylint: disable=line-too-long
    pp_common += '|keep("image", "labels")'
    FLAGS.config.pp_train = 'decode|resize_small(512)|central_crop(384)|flip_lr' + pp_common
    FLAGS.config.pp_eval = 'decode|resize(384)' + pp_common
    FLAGS.config.fewshot.pp_train = 'decode|resize_small(512)|central_crop(384)|value_range(-1,1)'
    FLAGS.config.fewshot.pp_eval = 'decode|resize(384)|value_range(-1,1)'
    with tfds.testing.mock_data(num_examples=100, data_dir=data_dir):
      train_loss, val_loss, fewshot_results = heteroscedastic.main(None)

    fewshot_acc_sum = sum(jax.tree_util.tree_flatten(fewshot_results)[0])
    logging.info('(train_loss, val_loss, fewshot_acc_sum) = %s, %s, %s',
                 train_loss, val_loss, fewshot_acc_sum)
    self.assertAllClose(train_loss, correct_train_loss)
    self.assertAllClose(val_loss, correct_val_loss)
    self.assertAllClose(fewshot_acc_sum, correct_fewshot_acc_sum)


if __name__ == '__main__':
  tf.test.main()