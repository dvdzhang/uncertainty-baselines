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

"""EfficientNet model adopted from official estimator version for tf2.

[1] Mingxing Tan, Quoc V. Le
  EfficientNet: Rethinking Model Scaling for Convolutional Neural Networks.
  ICML'19, https://arxiv.org/abs/1905.11946.
"""

import collections
import functools
import math
import tensorflow as tf
import utils  # local file import

BlockArgs = collections.namedtuple('BlockArgs', [
    'kernel_size',
    'num_repeat',
    'input_filters',
    'output_filters',
    'expand_ratio',
    'strides',
    'se_ratio',
])


def efficientnet_params(model_name):
  """Get efficientnet params based on model name."""
  params_dict = {
      # (width_coefficient, depth_coefficient, resolution, dropout_rate)
      'efficientnet-b0': (1.0, 1.0, 224, 0.2),
      'efficientnet-b1': (1.0, 1.1, 240, 0.2),
      'efficientnet-b2': (1.1, 1.2, 260, 0.3),
      'efficientnet-b3': (1.2, 1.4, 300, 0.3),
      'efficientnet-b4': (1.4, 1.8, 380, 0.4),
      'efficientnet-b5': (1.6, 2.2, 456, 0.4),
      'efficientnet-b6': (1.8, 2.6, 528, 0.5),
      'efficientnet-b7': (2.0, 3.1, 600, 0.5),
      'efficientnet-b8': (2.2, 3.6, 672, 0.5),
      'efficientnet-l2': (4.3, 5.3, 800, 0.5),
  }
  return params_dict[model_name]


def round_filters(filters, width_coefficient, depth_divisor, min_depth):
  """Round number of filters based on depth multiplier."""
  filters *= width_coefficient
  min_depth = min_depth or depth_divisor
  new_filters = max(
      min_depth,
      int(filters + depth_divisor / 2) // depth_divisor * depth_divisor)
  # Make sure that round down does not go down by more than 10%.
  if new_filters < 0.9 * filters:
    new_filters += depth_divisor
  return int(new_filters)


def round_repeats(repeats, depth_coefficient):
  """Round number of filters based on depth multiplier."""
  return int(math.ceil(depth_coefficient * repeats))


class MBConvBlock(tf.keras.layers.Layer):
  """A class of MBConv: Mobile Inverted Residual Bottleneck."""

  def __init__(self,
               block_args,
               batch_norm_momentum,
               batch_norm_epsilon,
               batch_norm,
               data_format,
               relu_fn,
               use_se,
               clip_projection_output):
    """Initializes a MBConv block.

    Args:
      block_args: BlockArgs, arguments to create a Block.
      batch_norm_momentum: Momentum for batch normalization.
      batch_norm_epsilon: Epsilon for batch normalization.
      batch_norm: Batch norm layer.
      data_format: Image data format.
      relu_fn: Activation.
      use_se: Whether to use squeeze and excitation layers.
      clip_projection_output: Whether to clip projected conv outputs.
    """
    super(MBConvBlock, self).__init__()
    self._block_args = block_args
    self._batch_norm_momentum = batch_norm_momentum
    self._batch_norm_epsilon = batch_norm_epsilon
    self._batch_norm = batch_norm
    self._data_format = data_format
    if self._data_format == 'channels_first':
      self._channel_axis = 1
      self._spatial_dims = [2, 3]
    else:
      self._channel_axis = -1
      self._spatial_dims = [1, 2]

    self._relu_fn = relu_fn
    self._has_se = (
        use_se and self._block_args.se_ratio is not None and
        0 < self._block_args.se_ratio <= 1)
    self._clip_projection_output = clip_projection_output
    self._build()

  def _build(self):
    """Builds block according to the arguments."""
    filters = self._block_args.input_filters * self._block_args.expand_ratio
    kernel_size = self._block_args.kernel_size
    self._expand_conv = tf.keras.layers.Conv2D(
        filters=filters,
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=utils.conv_kernel_initializer,
        padding='same',
        data_format=self._data_format,
        use_bias=False)
    self._bn0 = self._batch_norm(
        axis=self._channel_axis,
        momentum=self._batch_norm_momentum,
        epsilon=self._batch_norm_epsilon)
    self._depthwise_conv = tf.keras.layers.DepthwiseConv2D(
        kernel_size=[kernel_size, kernel_size],
        strides=self._block_args.strides,
        depthwise_initializer=utils.conv_kernel_initializer,
        padding='same',
        data_format=self._data_format,
        use_bias=False)
    self._bn1 = self._batch_norm(
        axis=self._channel_axis,
        momentum=self._batch_norm_momentum,
        epsilon=self._batch_norm_epsilon)
    if self._has_se:
      num_reduced_filters = max(
          1, int(self._block_args.input_filters * self._block_args.se_ratio))
      self._se_reduce = tf.keras.layers.Conv2D(
          num_reduced_filters,
          kernel_size=[1, 1],
          strides=[1, 1],
          kernel_initializer=utils.conv_kernel_initializer,
          padding='same',
          data_format=self._data_format,
          use_bias=True)
      self._se_expand = tf.keras.layers.Conv2D(
          filters,
          kernel_size=[1, 1],
          strides=[1, 1],
          kernel_initializer=utils.conv_kernel_initializer,
          padding='same',
          data_format=self._data_format,
          use_bias=True)

    filters = self._block_args.output_filters
    self._project_conv = tf.keras.layers.Conv2D(
        filters=filters,
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=utils.conv_kernel_initializer,
        padding='same',
        data_format=self._data_format,
        use_bias=False)
    self._bn2 = self._batch_norm(
        axis=self._channel_axis,
        momentum=self._batch_norm_momentum,
        epsilon=self._batch_norm_epsilon)

  def call(self, inputs, training=True, survival_prob=None):
    """Implementation of call().

    Args:
      inputs: the inputs tensor.
      training: boolean, whether the model is constructed for training.
      survival_prob: float, between 0 to 1, drop connect rate.

    Returns:
      A output tensor.
    """
    x = inputs
    if self._block_args.expand_ratio != 1:
      x = self._relu_fn(self._bn0(self._expand_conv(x), training=training))
    x = self._relu_fn(self._bn1(self._depthwise_conv(x), training=training))

    if self._has_se:
      se_tensor = tf.reduce_mean(
          x, self._spatial_dims, keepdims=True)
      se_tensor = self._se_expand(self._relu_fn(self._se_reduce(se_tensor)))
      x = tf.sigmoid(se_tensor) * x

    x = self._bn2(self._project_conv(x), training=training)
    # Add identity so that quantization-aware training can insert quantization
    # ops correctly.
    x = tf.identity(x)
    if self._clip_projection_output:
      x = tf.clip_by_value(x, -6, 6)
    if all(
        s == 1 for s in self._block_args.strides
    ) and self._block_args.input_filters == self._block_args.output_filters:
      if survival_prob:
        x = utils.drop_connect(x, training, survival_prob)
      x = tf.add(x, inputs)
    return x


class Model(tf.keras.Model):
  """EfficientNet."""

  def __init__(self,
               width_coefficient,
               depth_coefficient,
               dropout_rate,
               batch_norm_momentum=0.99,
               batch_norm_epsilon=1e-3,
               survival_prob=0.8,
               data_format='channels_last',
               num_classes=1000,
               depth_divisor=8,
               min_depth=None,
               relu_fn=tf.nn.swish,
               # TPU-specific requirement.
               batch_norm=tf.keras.layers.experimental.SyncBatchNormalization,
               use_se=True,
               clip_projection_output=False):
    """Initializes model instance.

    Args:
      width_coefficient: Coefficient to scale width.
      depth_coefficient: Coefficient to scale depth.
      dropout_rate: Dropout rate.
      batch_norm_momentum: Momentum for batch normalization.
      batch_norm_epsilon: Epsilon for batch normalization.
      survival_prob: float, survival probability for stochastic depth.
      data_format: Image data format.
      num_classes: Number of output classes.
      depth_divisor: Divisor to divide filters per conv when rounding.
      min_depth: Minimum depth per conv when rounding filters.
      relu_fn: Activation.
      batch_norm: Batch norm layer.
      use_se: Whether to use squeeze and excitation layers.
      clip_projection_output: Whether to clip projected conv outputs.
    """
    super(Model, self).__init__()
    self._width_coefficient = width_coefficient
    self._depth_coefficient = depth_coefficient
    self._dropout_rate = dropout_rate
    self._batch_norm_momentum = batch_norm_momentum
    self._batch_norm_epsilon = batch_norm_epsilon
    self._survival_prob = survival_prob
    self._data_format = data_format
    self._num_classes = num_classes
    self._depth_divisor = depth_divisor
    self._min_depth = min_depth
    self._relu_fn = relu_fn
    self._batch_norm = batch_norm
    self._use_se = use_se
    self._clip_projection_output = clip_projection_output
    self._build()

  def _build(self):
    """Builds a model."""
    if self._data_format == 'channels_first':
      channel_axis = 1
      self._spatial_dims = [2, 3]
    else:
      channel_axis = -1
      self._spatial_dims = [1, 2]

    self._conv_stem = tf.keras.layers.Conv2D(
        filters=round_filters(32,
                              self._width_coefficient,
                              self._depth_divisor,
                              self._min_depth),
        kernel_size=[3, 3],
        strides=[2, 2],
        kernel_initializer=utils.conv_kernel_initializer,
        padding='same',
        data_format=self._data_format,
        use_bias=False)
    self._bn0 = self._batch_norm(
        axis=channel_axis,
        momentum=self._batch_norm_momentum,
        epsilon=self._batch_norm_epsilon)

    Block = functools.partial(  # pylint: disable=invalid-name
        MBConvBlock,
        batch_norm_momentum=self._batch_norm_momentum,
        batch_norm_epsilon=self._batch_norm_epsilon,
        batch_norm=self._batch_norm,
        data_format=self._data_format,
        relu_fn=self._relu_fn,
        use_se=self._use_se,
        clip_projection_output=self._clip_projection_output)
    self._blocks = []
    blocks_args = [
        BlockArgs(kernel_size=3,
                  num_repeat=1,
                  input_filters=32,
                  output_filters=16,
                  expand_ratio=1,
                  strides=[1, 1],
                  se_ratio=0.25),
        BlockArgs(kernel_size=3,
                  num_repeat=2,
                  input_filters=16,
                  output_filters=24,
                  expand_ratio=6,
                  strides=[2, 2],
                  se_ratio=0.25),
        BlockArgs(kernel_size=5,
                  num_repeat=2,
                  input_filters=24,
                  output_filters=40,
                  expand_ratio=6,
                  strides=[2, 2],
                  se_ratio=0.25),
        BlockArgs(kernel_size=3,
                  num_repeat=3,
                  input_filters=40,
                  output_filters=80,
                  expand_ratio=6,
                  strides=[2, 2],
                  se_ratio=0.25),
        BlockArgs(kernel_size=5,
                  num_repeat=3,
                  input_filters=80,
                  output_filters=112,
                  expand_ratio=6,
                  strides=[1, 1],
                  se_ratio=0.25),
        BlockArgs(kernel_size=5,
                  num_repeat=4,
                  input_filters=112,
                  output_filters=192,
                  expand_ratio=6,
                  strides=[2, 2],
                  se_ratio=0.25),
        BlockArgs(kernel_size=3,
                  num_repeat=1,
                  input_filters=192,
                  output_filters=320,
                  expand_ratio=6,
                  strides=[1, 1],
                  se_ratio=0.25),
    ]
    for block_args in blocks_args:
      # Update block input and output filters based on depth multiplier.
      input_filters = round_filters(block_args.input_filters,
                                    self._width_coefficient,
                                    self._depth_divisor,
                                    self._min_depth)
      output_filters = round_filters(block_args.output_filters,
                                     self._width_coefficient,
                                     self._depth_divisor,
                                     self._min_depth)
      repeats = round_repeats(block_args.num_repeat,
                              self._depth_coefficient)
      block_args = block_args._replace(
          input_filters=input_filters,
          output_filters=output_filters,
          num_repeat=repeats)
      self._blocks.append(Block(block_args))

      if block_args.num_repeat > 1:
        # pylint: disable=protected-access
        block_args = block_args._replace(
            input_filters=block_args.output_filters, strides=[1, 1])
        # pylint: enable=protected-access
      for _ in range(block_args.num_repeat - 1):
        self._blocks.append(Block(block_args))

    self._conv_head = tf.keras.layers.Conv2D(
        filters=round_filters(1280,
                              self._width_coefficient,
                              self._depth_divisor,
                              self._min_depth),
        kernel_size=[1, 1],
        strides=[1, 1],
        kernel_initializer=utils.conv_kernel_initializer,
        padding='same',
        use_bias=False)
    self._bn1 = self._batch_norm(
        axis=channel_axis,
        momentum=self._batch_norm_momentum,
        epsilon=self._batch_norm_epsilon)
    self._avg_pooling = tf.keras.layers.GlobalAveragePooling2D(
        data_format=self._data_format)
    if self._dropout_rate > 0:
      self._dropout = tf.keras.layers.Dropout(self._dropout_rate)
    else:
      self._dropout = None
    self._fc = tf.keras.layers.Dense(
        self._num_classes,
        kernel_initializer=utils.dense_kernel_initializer)

  def call(self, inputs, training=True):
    """Implementation of call().

    Args:
      inputs: input tensors.
      training: boolean, whether the model is constructed for training.

    Returns:
      output tensors.
    """
    outputs = self._relu_fn(
        self._bn0(self._conv_stem(inputs), training=training))

    for idx, block in enumerate(self._blocks):
      survival_prob = self._survival_prob
      if survival_prob:
        drop_rate = 1.0 - survival_prob
        survival_prob = 1.0 - drop_rate * float(idx) / len(self._blocks)
      outputs = block.call(
          outputs, training=training, survival_prob=survival_prob)

    outputs = self._relu_fn(
        self._bn1(self._conv_head(outputs), training=training))
    outputs = self._avg_pooling(outputs)
    if self._dropout:
      outputs = self._dropout(outputs, training=training)
    outputs = self._fc(outputs)
    return outputs
