import os
import os.path as osp
import functools
import logging
import numpy as np
import tensorflow as tf
import tf_slim as slim
from uma_mot.tracker.Siamese_inference.convolutional_alexnet import convolutional_alexnet_arg_scope, convolutional_alexnet
from uma_mot.tracker.Siamese_utils.infer_utils import get_exemplar_images
from uma_mot.tracker.Siamese_utils.misc_utils import get_center

tf = tf.compat.v1
tf.disable_v2_behavior()

weight_decay = 5e-4
l2_reg = tf.keras.regularizers.L2(l2=weight_decay)

class InferenceWrapper():
  """Model wrapper class for performing inference with a siamese model."""

  def __init__(self, context_amount):
    self.image = None
    self.target_bbox_feed = None
    self.search_images = None
    self.embeds = None
    self.init_templates = None
    self.init = None
    self.model_config = None
    self.track_config = None
    self.response_up = None
    self.response = None
    self.frame_templates = None
    self.instance_feature = None
    self.reid_embeds = None
    self.context_amount = context_amount

  def build_graph_from_config(self, model_config, track_config, checkpoint_path):
    """Build the inference graph and return a restore function."""
    self.build_model(model_config, track_config)
    ema = tf.train.ExponentialMovingAverage(0)
    variables_to_restore = ema.variables_to_restore(moving_avg_variables=[])

    # Filter out State variables
    variables_to_restore_filterd = {}
    for key, value in variables_to_restore.items():
      if key.split('/')[1] != 'State':
        variables_to_restore_filterd[key] = value

    saver = tf.train.Saver(variables_to_restore_filterd)

    if osp.isdir(checkpoint_path):
      checkpoint_path = tf.train.latest_checkpoint(checkpoint_path)
      if not checkpoint_path:
        raise ValueError("No checkpoint file found in: {}".format(checkpoint_path))

    def _restore_fn(sess):
      logging.info("Loading model from checkpoint: %s", checkpoint_path)
      saver.restore(sess, checkpoint_path)
      logging.info("Successfully loaded checkpoint: %s", os.path.basename(checkpoint_path))

    return _restore_fn

  def build_model(self, model_config, track_config):
    self.model_config = model_config
    self.track_config = track_config

    self.build_inputs()
    self.build_search_images()
    self.build_template()
    self.build_detection()
    self.build_upsample()
    self.dumb_op = tf.no_op('dumb_operation')

  def build_inputs(self):
    # filename = tf.placeholder(tf.string, [], name='image')
    # image_file = tf.read_file(filename)
    # image = tf.image.decode_jpeg(image_file, channels=3, dct_method="INTEGER_ACCURATE")

    # image = tf.to_float(image)
    # print('img shape', image.shape)
    image = tf.placeholder(tf.float32, [None, None, 3], name='image')

    self.image = image
    self.target_bbox_feed = tf.placeholder(dtype=tf.float32,
                                           shape=[4],
                                           name='target_bbox_feed')  # center's y, x, height, width
    self.frame_templates = tf.placeholder(dtype=tf.float32, shape=[3, 6, 6, 256], name='frame_templates_feed') # track_feature

  def build_search_images(self):
    """Crop search images from the input image based on the last target position

    1. The input image is scaled such that the area of target&context takes up to (scale_factor * z_image_size) ^ 2
    2. Crop an image patch as large as x_image_size centered at the target center.
    3. If the cropped image region is beyond the boundary of the input image, mean values are padded.
    """
    model_config = self.model_config
    track_config = self.track_config

    size_z = model_config['z_image_size']   # 127
    size_x = track_config['x_image_size']   # 255

    num_scales = track_config['num_scales']   # 3
    scales = np.arange(num_scales) - get_center(num_scales)
    assert np.sum(scales) == 0, 'scales should be symmetric'
    search_factors = [track_config['scale_step'] ** x for x in scales]   # pow(1.0375, -1), pow(1.0375, 0), pow(1.0375, 1)

    frame_sz = tf.shape(self.image)
    target_yx = self.target_bbox_feed[0:2]
    target_size = self.target_bbox_feed[2:4]
    avg_chan = tf.reduce_mean(self.image, axis=(0, 1), name='avg_chan')

    # Compute base values
    base_z_size = target_size   # suppose [60, 120]
    base_z_context_size = base_z_size + self.context_amount * tf.reduce_sum(base_z_size)
    base_s_z = tf.sqrt(tf.reduce_prod(base_z_context_size))  # Canonical size, sqrt(87*147) = 113
    base_scale_z = tf.div(tf.to_float(size_z), base_s_z)  # 127 / 113 = 1.124
    d_search = (size_x - size_z) / 2.0  # 64
    base_pad = tf.div(d_search, base_scale_z)   # 64 / 1.124 =57
    base_s_x = base_s_z + 2 * base_pad   # 113 + 2*57 = 227
    base_scale_x = tf.div(tf.to_float(size_x), base_s_x)   # 255 / 227 = 1.123

    boxes = []

    for factor in search_factors:
      s_x = factor * base_s_x   # 1.0375 x 227
      frame_sz_1 = tf.to_float(frame_sz[0:2] - 1)
      # self.frame_shape = frame_sz_1
      topleft = tf.div(target_yx - get_center(s_x), frame_sz_1)
      bottomright = tf.div(target_yx + get_center(s_x), frame_sz_1)
      box = tf.concat([topleft, bottomright], axis=0)
      boxes.append(box)

    boxes = tf.stack(boxes)
    scale_xs = []
    for factor in search_factors:
      scale_x = base_scale_x / factor
      scale_xs.append(scale_x)
    self.scale_xs = tf.stack(scale_xs)

    # Note we use different padding values for each image
    # while the original implementation uses only the average value
    # of the first image for all images.
    image_minus_avg = tf.expand_dims(self.image - avg_chan, 0)
    image_cropped = tf.image.crop_and_resize(image_minus_avg, boxes,
                                             box_ind=tf.zeros((track_config['num_scales']), tf.int32),
                                             crop_size=[size_x, size_x])
    self.search_images = image_cropped + avg_chan

  def get_image_embedding(self, images, stage='init', reuse=None):

      config = self.model_config['embed_config']
      arg_scope = convolutional_alexnet_arg_scope(config,
                                                  trainable=config['train_embedding'],
                                                  is_training=False)

      @functools.wraps(convolutional_alexnet)
      def embedding_fn(images, stage, reuse=False):
        with slim.arg_scope(arg_scope):
          return convolutional_alexnet(images, stage=stage, reuse=reuse)

      track_feature, reid_feature_squeeze = embedding_fn(images, stage, reuse)
      return track_feature, reid_feature_squeeze

  def build_template(self):

    model_config = self.model_config
    track_config = self.track_config

    # Exemplar image lies at the center of the search image in the first frame
    exemplar_images = get_exemplar_images(self.search_images, [model_config['z_image_size'],
                                                               model_config['z_image_size']])

    self.exemplar = exemplar_images
    templates, reid_templates = self.get_image_embedding(exemplar_images, stage='init')

    center_scale = int(get_center(track_config['num_scales']))
    center_template = tf.identity(templates[center_scale]) # Shared feature
    self.center_template = center_template
    self.reid_templates = tf.identity(reid_templates[center_scale])


    templates = tf.stack([center_template for _ in range(track_config['num_scales'])])

    with tf.variable_scope('target_template'):
      # Store template in Variable such that we don't have to feed this template every time.
      with tf.variable_scope('State'):
        state = tf.get_variable('exemplar',
                                initializer=tf.zeros(templates.get_shape().as_list(), dtype=templates.dtype),
                                trainable=False)
        with tf.control_dependencies([templates]):
          self.init = tf.assign(state, templates, validate_shape=True)
        self.init_templates = state

  def build_detection(self):  #  co-relation

    self.embeds, self.reid_embeds = self.get_image_embedding(self.search_images, stage='track', reuse=True)   # [3, 22, 22, 256]

    with tf.variable_scope('detection'):
      def _translation_match(x, z):
        x = tf.expand_dims(x, 0)  # [batch, in_height, in_width, in_channels]
        z = tf.expand_dims(z, -1)  # [filter_height, filter_width, in_channels, out_channels]
        return tf.nn.conv2d(x, z, strides=[1, 1, 1, 1], padding='VALID', name='translation_match')

      output = tf.map_fn(
        lambda x: _translation_match(x[0], x[1]),
        (self.embeds, self.frame_templates), dtype=self.embeds.dtype)  # of shape [3, 1, 17, 17, 1]
      output = tf.squeeze(output, [1, 4])  # of shape e.g. [3, 17, 17]

      bias = tf.get_variable('biases', [1],
                             dtype=tf.float32,
                             initializer=tf.constant_initializer(0.0, dtype=tf.float32),
                             trainable=False)
      response = self.model_config['adjust_response_config']['scale'] * output + bias
      self.response = response

  def build_upsample(self):
    """Upsample response to obtain finer target position"""
    with tf.variable_scope('upsample'):
      response = tf.expand_dims(self.response, 3)  # [3,17,17,1]
      up_method = self.track_config['upsample_method']
      methods = {'bilinear': tf.image.ResizeMethod.BILINEAR,
                 'bicubic': tf.image.ResizeMethod.BICUBIC}
      up_method = methods[up_method]
      response_spatial_size = self.response.get_shape().as_list()[1:3]
      up_size = [s * self.track_config['upsample_factor'] for s in response_spatial_size]  # [272,272]
      response_up = tf.image.resize_images(response,
                                           up_size,
                                           method=up_method,
                                           align_corners=True)
      response_up = tf.squeeze(response_up, [3])   # [3, 272, 272]
      self.response_up = response_up

  def initialize(self, sess, input_feed):
    image, target_bbox = input_feed
    _, _,  reid_templates = sess.run([self.scale_xs, self.init, self.reid_templates], feed_dict={'image:0': image, "target_bbox_feed:0": target_bbox, })
    init_templates = self.init_templates.eval(session=sess)

    return init_templates, reid_templates

  def inference_step(self, sess, input_feed):
    image, target_bbox, frame_templates = input_feed  #  input_feed = [filename, bbox_feed, templates]
    log_level = self.track_config['log_level']
    image_cropped_op = self.search_images if log_level > 0 else self.dumb_op
    image_cropped, scale_xs, response_up, instance_track, instance_reid = sess.run(
      fetches=[image_cropped_op, self.scale_xs, self.response_up, self.embeds, self.reid_embeds],
      feed_dict={
        "image:0": image,
        "target_bbox_feed:0": target_bbox,
        "frame_templates_feed:0": frame_templates})

    output = {
      'scale_xs': scale_xs,
      'response_up': response_up,
      'instance': instance_track,
      'instance_reid': instance_reid
      }
    return output




