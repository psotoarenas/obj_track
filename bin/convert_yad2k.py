"""
Reads Darknet19 config and weights and creates Keras model with TF backend.
Currently only supports layers in Darknet19 config.

YOLO directory structure:
- models
|-- research : where the tensorflow api is stored
|-- yolo : yolo_dir
|   |-- cfg : folder to store the .cfg file to be downloaded
|   |-- weights : folder to store the .weights file to be downloaded
|   |-- data : folder where the converted model, the labels and the anchors
               are stored
"""

import argparse
import configparser
import io
import os
import wget
from collections import defaultdict
import numpy as np
from keras import backend as K
from keras.layers import (Conv2D, GlobalAveragePooling2D, Input, Lambda,
                          MaxPooling2D, ZeroPadding2D, Add, UpSampling2D,
                          Concatenate)
from keras.layers.advanced_activations import LeakyReLU
from keras.layers.merge import concatenate
from keras.layers.normalization import BatchNormalization
from keras.models import Model
from keras.regularizers import l2
from keras.utils.vis_utils import plot_model as plot

from obj_track.yad2k.models.keras_yolov2 import space_to_depth_x2, \
    space_to_depth_x2_output_shape

parser = argparse.ArgumentParser(
    description='Yet Another Darknet To Keras Converter.')
parser.add_argument('-v', '--version', help='Version of YOLO to download')
parser.add_argument(
    '-flcl',
    '--fully_convolutional',
    help='Model is fully convolutional so set input shape to (None, None, 3). '
    'WARNING: This experimental option does not work properly for YOLO_v2.',
    action='store_true')


def unique_config_sections(config_file):
    """Convert all config sections to have unique names.
    Adds unique suffixes to config sections for compability with configparser.
    """
    section_counters = defaultdict(int)
    output_stream = io.StringIO()
    with open(config_file) as fin:
        for line in fin:
            if line.startswith('['):
                section = line.strip().strip('[]')
                _section = section + '_' + str(section_counters[section])
                section_counters[section] += 1
                line = line.replace(section, _section)
            output_stream.write(line)
    output_stream.seek(0)
    return output_stream


# %%
def _main(args):
    # todo-paola: delete the following line when executing from root directory
    os.chdir("..")

    # make all dirs needed, run from root
    working_dir = os.getcwd()
    yolo_dir = os.path.join(working_dir, "models", "yolo")
    os.makedirs(yolo_dir, exist_ok=True)
    os.makedirs(yolo_dir+'/cfg', exist_ok=True)
    os.makedirs(yolo_dir+'/weights', exist_ok=True)
    os.makedirs(yolo_dir+'/data', exist_ok=True)
    yolo_version = args.version
    cfg_url_base = 'https://raw.github.com/pjreddie/darknet/master/cfg/'
    weight_url_base = 'https://pjreddie.com/media/files/'

    # Get the .cfg file
    url = cfg_url_base + yolo_version + '.cfg'
    config_path = os.path.join(yolo_dir, 'cfg')
    # download the url contents in binary
    # todo-paola: uncomment the following line and delete the hardcoded path
    print("Downloading .cfg file from {}".format(url))
    config_path = wget.download(url, out=config_path)
    #config_path = config_path + '/yolov2.cfg'

    # Get the .weights file
    url = weight_url_base + yolo_version + '.weights'
    weights_path = os.path.join(yolo_dir, 'weights')
    # download the url contents in binary format
    # todo-paola: uncomment the following line and delete the hardcoded
    print("Downloading .weights file from {}".format(url))
    weights_path = wget.download(url, out=weights_path)
    #weights_path = weights_path + '/yolov2.weights'

    assert config_path.endswith('.cfg'), '{} is not a .cfg file'.format(
        config_path)
    assert weights_path.endswith(
        '.weights'), '{} is not a .weights file'.format(weights_path)

    output_path = os.path.join(yolo_dir, 'data')
    output_path = os.path.join(output_path, yolo_version + '.h5')
    assert output_path.endswith('.h5'), \
        'output path {} is not a .h5 file'.format(output_path)
    output_root = os.path.splitext(output_path)[0]

    # Load weights and config.
    print('Loading weights.')
    weights_file = open(weights_path, 'rb')
    if yolo_version.endswith('v2'):
        weights_header = np.ndarray(
            shape=(4, ), dtype='int32', buffer=weights_file.read(16))
        print('Weights Header: ', weights_header)
    else:
        major, minor, revision = np.ndarray(
            shape=(3,), dtype='int32', buffer=weights_file.read(12))
        if (major * 10 + minor) >= 2 and major < 1000 and minor < 1000:
            seen = np.ndarray(shape=(1,), dtype='int64',
                              buffer=weights_file.read(8))
        else:
            seen = np.ndarray(shape=(1,), dtype='int32',
                              buffer=weights_file.read(4))
        print('Weights Header: ', major, minor, revision, seen)


    print('Parsing Darknet config.')
    unique_config_file = unique_config_sections(config_path)
    cfg_parser = configparser.ConfigParser()
    cfg_parser.read_file(unique_config_file)

    print('Creating Keras model.')
    if args.fully_convolutional:
        image_height, image_width = None, None
    else:
        image_height = int(cfg_parser['net_0']['height'])
        image_width = int(cfg_parser['net_0']['width'])


    if yolo_version.endswith('v2'):
        prev_layer = Input(shape=(image_height, image_width, 3))
        all_layers = [prev_layer]
    else:
        input_layer = Input(shape=(None, None, 3))
        prev_layer = input_layer
        all_layers = []
        out_index = []

    weight_decay = float(cfg_parser['net_0']['decay']
                         ) if 'net_0' in cfg_parser.sections() else 5e-4
    count = 0
    for section in cfg_parser.sections():
        print('Parsing section {}'.format(section))
        if section.startswith('convolutional'):
            filters = int(cfg_parser[section]['filters'])
            size = int(cfg_parser[section]['size'])
            stride = int(cfg_parser[section]['stride'])
            pad = int(cfg_parser[section]['pad'])
            activation = cfg_parser[section]['activation']
            batch_normalize = 'batch_normalize' in cfg_parser[section]

            if yolo_version.endswith('v2'):
                # padding='same' is equivalent to Darknet pad=1
                padding = 'same' if pad == 1 else 'valid'
            else:
                padding = 'same' if pad == 1 and stride == 1 else 'valid'

            # Setting weights.
            # Darknet serializes convolutional weights as:
            # [bias/beta, [gamma, mean, variance], conv_weights]
            prev_layer_shape = K.int_shape(prev_layer)

            # TODO: This assumes channel last dim_ordering.
            weights_shape = (size, size, prev_layer_shape[-1], filters)
            darknet_w_shape = (filters, weights_shape[2], size, size)
            weights_size = np.product(weights_shape)

            print('conv2d', 'bn'
                  if batch_normalize else '  ', activation, weights_shape)

            conv_bias = np.ndarray(
                shape=(filters, ),
                dtype='float32',
                buffer=weights_file.read(filters * 4))
            count += filters

            if batch_normalize:
                bn_weights = np.ndarray(
                    shape=(3, filters),
                    dtype='float32',
                    buffer=weights_file.read(filters * 12))
                count += 3 * filters

                # TODO: Keras BatchNormalization mistakenly refers to var
                # as std.
                bn_weight_list = [
                    bn_weights[0],  # scale gamma
                    conv_bias,  # shift beta
                    bn_weights[1],  # running mean
                    bn_weights[2]  # running var
                ]

            conv_weights = np.ndarray(
                shape=darknet_w_shape,
                dtype='float32',
                buffer=weights_file.read(weights_size * 4))
            count += weights_size

            # DarkNet conv_weights are serialized Caffe-style:
            # (out_dim, in_dim, height, width)
            # We would like to set these to Tensorflow order:
            # (height, width, in_dim, out_dim)
            # TODO: Add check for Theano dim ordering.
            conv_weights = np.transpose(conv_weights, [2, 3, 1, 0])
            conv_weights = [conv_weights] if batch_normalize else [
                conv_weights, conv_bias
            ]

            # Handle activation.
            act_fn = None
            if activation == 'leaky':
                pass  # Add advanced activation later.
            elif activation != 'linear':
                raise ValueError(
                    'Unknown activation function `{}` in section {}'.format(
                        activation, section))

            if stride > 1:
                # Darknet uses left and top padding instead of 'same' mode
                prev_layer = ZeroPadding2D(((1, 0), (1, 0)))(prev_layer)
            # Create Conv2D layer
            conv_layer = (Conv2D(
                filters, (size, size),
                strides=(stride, stride),
                kernel_regularizer=l2(weight_decay),
                use_bias=not batch_normalize,
                weights=conv_weights,
                activation=act_fn,
                padding=padding))(prev_layer)

            if batch_normalize:
                conv_layer = (BatchNormalization(
                    weights=bn_weight_list))(conv_layer)
            prev_layer = conv_layer

            if activation == 'linear':
                all_layers.append(prev_layer)
            elif activation == 'leaky':
                act_layer = LeakyReLU(alpha=0.1)(prev_layer)
                prev_layer = act_layer
                all_layers.append(act_layer)

        elif section.startswith('maxpool'):
            size = int(cfg_parser[section]['size'])
            stride = int(cfg_parser[section]['stride'])
            all_layers.append(MaxPooling2D(padding='same', pool_size=
            (size, size), strides=(stride, stride))(prev_layer))
            prev_layer = all_layers[-1]

        elif section.startswith('avgpool'):
            if cfg_parser.items(section) != []:
                raise ValueError('{} with params unsupported.'.format(section))
            all_layers.append(GlobalAveragePooling2D()(prev_layer))
            prev_layer = all_layers[-1]

        elif section.startswith('route'):
            ids = [int(i) for i in cfg_parser[section]['layers'].split(',')]
            layers = [all_layers[i] for i in ids]
            if len(layers) > 1:
                print('Concatenating route layers:', layers)
                concatenate_layer = concatenate(layers)
                all_layers.append(concatenate_layer)
                prev_layer = concatenate_layer
            else:
                skip_layer = layers[0]  # only one layer to route
                all_layers.append(skip_layer)
                prev_layer = skip_layer

        elif section.startswith('reorg'):
            block_size = int(cfg_parser[section]['stride'])
            assert block_size == 2, 'Only reorg with stride 2 supported.'
            all_layers.append(
                Lambda(
                    space_to_depth_x2,
                    output_shape=space_to_depth_x2_output_shape,
                    name='space_to_depth_x2')(prev_layer))
            prev_layer = all_layers[-1]

        elif section.startswith('shortcut'):
            index = int(cfg_parser[section]['from'])
            activation = cfg_parser[section]['activation']
            assert activation == 'linear', 'Only linear activation supported.'
            all_layers.append(Add()([all_layers[index], prev_layer]))
            prev_layer = all_layers[-1]

        elif section.startswith('upsample'):
            stride = int(cfg_parser[section]['stride'])
            assert stride == 2, 'Only stride=2 supported.'
            all_layers.append(UpSampling2D(stride)(prev_layer))
            prev_layer = all_layers[-1]

        elif section.startswith('yolo'):
            with open('{}_anchors.txt'.format(output_root), 'w') as f:
                print(cfg_parser[section]['anchors'], file=f)
            out_index.append(len(all_layers) - 1)
            all_layers.append(None)
            prev_layer = all_layers[-1]

        elif section.startswith('region'):
            with open('{}_anchors.txt'.format(output_root), 'w') as f:
                print(cfg_parser[section]['anchors'], file=f)

        elif (section.startswith('net') or section.startswith('cost') or
              section.startswith('softmax')):
            pass  # Configs not currently handled during model definition.

        else:
            raise ValueError(
                'Unsupported section header type: {}'.format(section))

    # Create and save model.
    if yolo_version.endswith('v2'):
        model = Model(inputs=all_layers[0], outputs=all_layers[-1])
    else:
        if len(out_index) == 0: out_index.append(len(all_layers) - 1)
        model = Model(inputs=input_layer, outputs=[all_layers[i] for i in
                                                   out_index])
    print(model.summary())
    # Save model summary in .txt file
    with open(output_root + '_summary.txt', 'w') as fh:
        # Pass the file handle in as a lambda function to make it callable
        model.summary(print_fn=lambda x: fh.write(x + '\n'))

    model.save('{}'.format(output_path))
    print('Saved Keras model to {}'.format(output_path))
    # Check to see if all weights have been read.
    remaining_weights = len(weights_file.read()) / 4
    weights_file.close()
    print('Read {} of {} from Darknet weights.'.format(count, count +
                                                       remaining_weights))
    if remaining_weights > 0:
        print('Warning: {} unused weights'.format(remaining_weights))

if __name__ == '__main__':
    _main(parser.parse_args())
