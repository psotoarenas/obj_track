"""
Run a YOLOv3 detection on video.
Modified version of https://github.com/Adamdad/keras-YOLOv3-mobilenet.git
"""

import colorsys
from timeit import default_timer as timer

import numpy as np
from keras import backend as K
from keras.models import load_model
from keras.layers import Input
from PIL import Image, ImageFont, ImageDraw

from ..yad2k.models.keras_yolov3 import  yolo_eval, yolo_body, tiny_yolo_body
from ..yad2k.utils.utils_yolo_v3 import letterbox_image, letterbox_image_cv
import os
from keras.utils import multi_gpu_model
import cv2

from .utils import get_video_props

class YOLO(object):
    _defaults = {
        "classes_path" : " ",
        "detector" : "yolov3",
        "score" : 0.3,
        "iou" : 0.5,
        "model_image_size" : (416, 416),
        "gpu_num" : 1,
    }

    @classmethod
    def get_defaults(cls, n):
        if n in cls._defaults:
            return cls._defaults[n]
        else:
            return "Unrecognized attribute name '" + n + "'"

    def __init__(self, **kwargs):
        self.__dict__.update(self._defaults) # set up default values
        self.__dict__.update(kwargs) # and update with user overrides
        self._set_paths()
        self.class_names = self._get_class()
        self.anchors = self._get_anchors()
        self.sess = K.get_session()
        self.boxes, self.scores, self.classes = self.generate()

    def _get_class(self):
        classes_path = os.path.expanduser(self.classes_path)
        with open(classes_path) as f:
            class_names = f.readlines()
        class_names = [c.strip() for c in class_names]
        return class_names

    def _set_paths(self):
        # --------------------------------------------------------------------#
        #  Get and load converted yolo model with anchors and classes and     #
        #  labels                                                             #
        # --------------------------------------------------------------------#

        # todo-paola: delete the following line when executing from root
        #  directory
        os.chdir("..")

        # Get proper files, model, anchors and labels.
        root_dir = os.getcwd()
        yolo_data_dir = os.path.join(root_dir, "models", "yolo", "data")
        model_path = None
        classes_path = None
        anchors_path = None
        files = os.listdir(yolo_data_dir)
        assert files, 'There are no files in yolo/data directory, ' \
                      'run convert_yad2k.py first'
        yolo_version = self.detector
        model_name = yolo_version + '.h5'
        anchors_name = yolo_version + '_anchors.txt'
        for file in files:
            if file == model_name:
                model_path = os.path.join(yolo_data_dir, file)
            if file.endswith('classes.txt'):
                classes_path = os.path.join(yolo_data_dir, file)
            if file == anchors_name:
                anchors_path = os.path.join(yolo_data_dir, file)

        assert model_path.endswith('.h5'), 'Keras model must be a .h5 file.'
        assert anchors_path.endswith(
            'anchors.txt'), 'An *_anchors.txt file must ' \
                            'be provided'
        assert classes_path.endswith(
            'classes.txt'), 'classes for dataset must be ' \
                            'provided in .txt file'
        self.model_path = model_path
        self.classes_path = classes_path
        self.anchors_path = anchors_path

    def _get_model(self):
        pass

    def _get_anchors(self):
        anchors_path = os.path.expanduser(self.anchors_path)
        with open(anchors_path) as f:
            anchors = f.readline()
        anchors = [float(x) for x in anchors.split(',')]
        return np.array(anchors).reshape(-1, 2)

    def generate(self):
        model_path = os.path.expanduser(self.model_path)
        assert model_path.endswith('.h5'), \
            'Keras model or weights must be a .h5 file.'

        # Load model, or construct model and load weights.
        num_anchors = len(self.anchors)
        num_classes = len(self.class_names)
        is_tiny_version = num_anchors==6 # default setting
        try:
            self.yolo_model = load_model(model_path, compile=False)
        except:
            self.yolo_model = tiny_yolo_body(Input(shape=(None,None,3)),
                                             num_anchors//2, num_classes) \
                if is_tiny_version else yolo_body(Input(shape=(None,None,3)),
                                                  num_anchors//3, num_classes)
            self.yolo_model.load_weights(self.model_path) # make sure model, anchors and classes match
        else:
            assert self.yolo_model.layers[-1].output_shape[-1] == \
                num_anchors/len(self.yolo_model.output) * (num_classes + 5), \
                'Mismatch between model and given anchor and class sizes'

        print('{} model, anchors, and classes loaded.'.format(model_path))

        # Generate colors for drawing bounding boxes.
        hsv_tuples = [(x / len(self.class_names), 1., 1.)
                      for x in range(len(self.class_names))]
        self.colors = list(map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
        self.colors = list(
            map(lambda x: (int(x[0] * 255), int(x[1] * 255), int(x[2] * 255)),
                self.colors))
        np.random.seed(10101)  # Fixed seed for consistent colors across runs.
        np.random.shuffle(self.colors)  # Shuffle colors to decorrelate adjacent classes.
        np.random.seed(None)  # Reset seed to default.

        # Generate output tensor targets for filtered bounding boxes.
        self.input_image_shape = K.placeholder(shape=(2, ))
        if self.gpu_num>=2:
            self.yolo_model = multi_gpu_model(self.yolo_model,
                                              gpus=self.gpu_num)
        boxes, scores, classes = yolo_eval(self.yolo_model.output, self.anchors,
                len(self.class_names), self.input_image_shape,
                score_threshold=self.score, iou_threshold=self.iou)
        return boxes, scores, classes

    def detect_image(self, image):
        start = timer()

        if self.model_image_size != (None, None):
            assert self.model_image_size[0]%32 == 0, 'Multiples of 32 required'
            assert self.model_image_size[1]%32 == 0, 'Multiples of 32 required'
            boxed_image = letterbox_image_cv(image, tuple(reversed(
                self.model_image_size)))
        else:
            height, width, _ = image.shape
            new_image_size = (width - (width % 32), height - (height % 32))
            boxed_image = letterbox_image(image, new_image_size)
        image_data = np.array(boxed_image, dtype='float32')

        image_data /= 255.
        image_data = np.expand_dims(image_data, 0)  # Add batch dimension.

        out_boxes, out_scores, out_classes = self.sess.run(
            [self.boxes, self.scores, self.classes],
            feed_dict={
                self.yolo_model.input: image_data,
                self.input_image_shape: [image.shape[0], image.shape[1]],
                K.learning_phase(): 0
            })

        font = 0
        fontSize = 1e-3 * image.shape[0]
        thickness = int((image.shape[1] + image.shape[0]) // 300)

        for i, c in reversed(list(enumerate(out_classes))):
            predicted_class = self.class_names[c]
            box = out_boxes[i]
            score = out_scores[i]

            label = '{} {:.2f}'.format(predicted_class, score)

            top, left, bottom, right = box
            top = max(0, np.floor(top + 0.5).astype('int32'))
            left = max(0, np.floor(left + 0.5).astype('int32'))
            bottom = min(image.shape[0], np.floor(bottom + 0.5).astype('int32'))
            right = min(image.shape[1], np.floor(right + 0.5).astype('int32'))

            cv2.rectangle(image, (left, top), (right, bottom),
                              self.colors[c], thickness)
            cv2.putText(image, label, (left, top - 12), font, fontSize,
                        self.colors[c], thickness)
        return image

    def close_session(self):
        self.sess.close()

def yolo_v3(yolo, video_path, output_path=""):
    import cv2
    # -----------------------------------------------------------------------#
    #                          Configure OpenCV                              #
    # ------------------------------------------q-----------------------------#
    if video_path == '0':
        video_path = 0

    vid = cv2.VideoCapture(video_path)

    # todo-paola: add show option to parser
    show = False

    # if http address is not reachable assertion error will be raised
    assert vid.isOpened(), "Couldn't open video source"

    video_FourCC = cv2.VideoWriter_fourcc(*'XVID')
    video_fps = vid.get(cv2.CAP_PROP_FPS)
    video_size = (int(vid.get(cv2.CAP_PROP_FRAME_WIDTH)),
                  int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    isOutput = True if output_path != "" else False
    if isOutput:
        # print("!!! TYPE:", type(output_path), type(video_FourCC),
        #       type(video_fps), type(video_size))
        out = cv2.VideoWriter(output_path+"/output.avi", video_FourCC,
                              video_fps, video_size)
    accum_time = 0
    curr_fps = 0
    fps = "FPS: ??"
    prev_time = timer()
    while vid.isOpened():
        return_value, frame = vid.read()
        if frame is None:
            print('\nEnd of Video')
            break
        image = yolo.detect_image(frame)
        curr_time = timer()
        exec_time = curr_time - prev_time
        prev_time = curr_time
        accum_time = accum_time + exec_time
        curr_fps = curr_fps + 1
        if accum_time > 1:
            accum_time = accum_time - 1
            fps = "FPS: " + str(curr_fps)
            curr_fps = 0
        cv2.putText(image, text=fps, org=(3, 15),
                    fontFace=cv2.FONT_HERSHEY_SIMPLEX, fontScale=0.50,
                    color=(255, 0, 0), thickness=2)
        if show:
            cv2.namedWindow("result", cv2.WINDOW_NORMAL)
            cv2.imshow("result", image)
        if isOutput:
            out.write(image)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    yolo.close_session()
    print('Job finished')