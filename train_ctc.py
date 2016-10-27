import sys
import os
import yaml

sys.path = [os.path.join('keras')] + sys.path

import tensorflow as tf
# Import CTC loss
# try:
#     from tensorflow.contrib.warpctc.ctc_ops import warp_ctc_loss as ctc_loss
#     print('Using warp ctc :)')
# except ImportError:
from tensorflow.python.ops.ctc_ops import ctc_loss
print('Using tf ctc :(')

import numpy as np
import librosa
from scipy import sparse
import argparse
import h5py
import uuid
import cPickle as pickle

import keras
import keras.backend as K
from keras.callbacks import Callback, ModelCheckpoint
from keras.models import Model
from keras.layers import Input, Dense, Activation, Lambda, TimeDistributed, LSTM
from keras.optimizers import RMSprop, SGD
from keras.utils.data_utils import get_file
from layers import RHN

from preprocess_timit import get_char_map, get_phn_map

def ler(y_true, y_pred, **kwargs):
    return tf.reduce_mean(tf.edit_distance(y_pred, y_true, **kwargs))

def decode(inputs, **kwargs):
    import tensorflow as tf
    is_greedy = kwargs.get('is_greedy', True)
    y_pred, seq_len = inputs

    seq_len = tf.cast(seq_len[:, 0], tf.int32)
    y_pred = tf.transpose(y_pred, perm=[1, 0, 2])

    if is_greedy:
        decoded = tf.nn.ctc_greedy_decoder(y_pred, seq_len)[0][0]
    else:
        decoded = tf.nn.ctc_beam_search_decoder(y_pred, seq_len)[0][0]

    return decoded

def decode_output_shape(inputs_shape):
    y_pred_shape, seq_len_shape = inputs_shape
    return (y_pred_shape[:1], None)

def get_inv_dict(dict_label):
    inv_dict = {v: k for (k, v) in dict_label.iteritems()}
    # Add blank label
    inv_dict[len(inv_dict)] = '<b>'
    return inv_dict

def get_output(model, x):
    return ''.join([inv_dict[i] for i in model.predict(x).argmax(axis=2)[0]])

def get_from_h5(h5_file, dataset, label_type='phn'):
    X = np.array(h5_file['%s/inputs/data' %dataset])
    seq_len = np.array(h5_file['%s/inputs/seq_len' %dataset])

    values = np.array(h5_file['%s/%s/values' %(dataset, label_type)])
    indices = np.array(h5_file['%s/%s/indices' %(dataset, label_type)])
    indices = (indices[:, 0], indices[:, 1])
    shape = np.array(h5_file['%s/%s/shape' %(dataset, label_type)])

    y = sparse.coo_matrix((values, indices), shape=shape).tolil()
    return X, seq_len, y

def ctc_lambda_func(args):
    y_pred, labels, input_length = args
    import tensorflow as tf
    return tf.nn.ctc_loss(tf.transpose(y_pred, perm=[1, 0, 2]), labels, input_length[:, 0])

def ctc_dummy_loss(y_true, y_pred):
    return y_pred

def decoder_dummy_loss(y_true, y_pred):
    return K.zeros((1,))

def treta_loader(treta_path):
    keras.metrics.ler = ler
    keras.objectives.decoder_dummy_loss = decoder_dummy_loss
    keras.objectives.ctc_dummy_loss = ctc_dummy_loss
    from layers import RHN, highway_bias_initializer
    keras.initializations.highway_bias_initializer = highway_bias_initializer
    modelin = keras.models.load_model(treta_path, custom_objects={'RHN':RHN})
    return modelin

class MetaCheckpoint(Callback):
    '''
    Checkpoints some training information on a meta file. Together with the
    Keras model saving, this should enable resuming training and having training
    information on every checkpoint.
    '''

    def __init__(self, filepath, schedule=None, training_args=None):
        self.filepath = filepath
        self.meta = {'epoch': []}
        if schedule:
            self.meta['schedule'] = schedule.get_config()
        if training_args:
            self.meta['training_args'] = training_args

    def on_train_begin(self, logs={}):
        self.epoch_offset = len(self.meta['epoch'])

    def on_epoch_end(self, epoch, logs={}):
        # Get statistics
        self.meta['epoch'].append(epoch + self.epoch_offset)
        for k, v in logs.items():
            # Get default gets the value or sets (and gets) the default value
            self.meta.setdefault(k, []).append(v)

        # Save to file
        filepath = self.filepath.format(epoch=epoch, **logs)

        with open(filepath, 'wb') as f:
            yaml.dump(self.meta, f)


def ctc_model(nb_features, nb_hidden, nb_layers, layer_norm, nb_classes, dropout):
    ctc = Lambda(ctc_lambda_func, output_shape=(1,), name="ctc")
    dec = Lambda(decode, output_shape=decode_output_shape,
                 arguments={'is_greedy': True}, name='decoder')

    # Define placeholders
    x = Input(name='input', shape=(None, nb_features))
    labels = Input(name='labels', shape=(None,), dtype='int32', sparse=True)
    input_length = Input(name='input_length', shape=(None,), dtype='int32')

    # Define model
    o = x
    if args.layer == 'rhn':
        o = RHN(nb_hidden, nb_layers=nb_layers,
                return_sequences=True, layer_norm=layer_norm, dropout_W=dropout, dropout_U=dropout)(o)
    else:
        for l in xrange(args.nb_layers):
            o = RNN(nb_hidden, return_sequences=True, consume_less='gpu', dropout_W=dropout, dropout_U=dropout)(o)

    o = TimeDistributed(Dense(nb_classes))(o)
    # Define loss as a layer
    l = ctc([o, labels, input_length])
    y_pred = dec([o, input_length])

    model = Model(input=[x, labels, input_length], output=[l, y_pred])
    return model

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='CTC Models training with TIMIT dataset')
    parser.add_argument('--layer', type=str, choices=['lstm', 'rhn', 'rnn',
                                                      'gru'], default='lstm')
    parser.add_argument('--nb_layers', type=int, default=3)
    parser.add_argument('--layer_norm', action='store_true', default=False)
    parser.add_argument('--nb_hidden', type=int, default=250)
    parser.add_argument('--batch_size', type=int, default=64)
    parser.add_argument('--nb_epoch', type=int, default=250)
    parser.add_argument('--label_type', type=str, choices=['phn', 'char'], default='phn')
    parser.add_argument('--lr', type=float, default=1e-4)
    parser.add_argument('--clipnorm', type=float, default=10.)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--gpu', default='0', type=str)
    parser.add_argument('--dropout', default=0., type=float)
    parser.add_argument('--save', default=os.path.join('results', str(uuid.uuid1())), type=str)
    parser.add_argument('--load', default=None)
    args = parser.parse_args()

    if args.gpu == '-1':
        config = tf.ConfigProto(device_count = {'GPU': 0})
    else:
        if args.gpu == 'all':
            args.gpu = ''
        config = tf.ConfigProto()
        config.gpu_options.visible_device_list = args.gpu

    session = tf.Session(config=config)
    K.set_session(session)

    # Read dataset
    with h5py.File('timit.h5', 'r') as f:
        X, seq_len, y = get_from_h5(f, 'train', label_type=args.label_type)
        X_valid, seq_len_valid, y_valid = get_from_h5(f, 'valid', label_type=args.label_type)
        X_test, seq_len_test, y_test = get_from_h5(f, 'test', label_type=args.label_type)

    if args.label_type == 'phn':
        _, dict_label = get_phn_map('timit/phones.60-48-39.map')
    else:
        dict_label = get_char_map()

    inv_dict = get_inv_dict(dict_label)

    nb_features = X.shape[2]
    nb_classes = len(inv_dict)

    if args.layer == 'lstm':
        RNN = LSTM
    elif args.layer == 'gru':
        RNN = GRU
    elif args.layer == 'rnn':
        RNN = SimpleRNN
    elif args.layer == 'rhn':
        RNN = RHN

    if args.load is None:
        model = ctc_model(nb_features, args.nb_hidden, args.nb_layers, args.layer_norm, nb_classes, args.dropout)
    else:
        model = treta_loader(args.load)

    # Optimization
    opt = SGD(lr=args.lr, momentum=args.momentum, clipnorm=args.clipnorm)

    # Compile with dummy loss
    model.compile(loss={'ctc': ctc_dummy_loss,
                        'decoder': decoder_dummy_loss},
                  optimizer=opt, metrics={'decoder': ler},
                  loss_weights=[1, 0])


    # Define callbacks
    name = args.save
    if not os.path.isdir(name):
        os.makedirs(name)

    meta_ckpt = MetaCheckpoint(os.path.join(name, 'meta.yaml'), training_args=vars(args))
    model_ckpt = ModelCheckpoint(os.path.join(name, 'model.h5'))
    best_ckpt = ModelCheckpoint(os.path.join(name, 'best.h5'), monitor='val_decoder_ler', save_best_only=True, mode='min')
    callback_list = [meta_ckpt, model_ckpt, best_ckpt]

    #  Fit the model
    model.fit([X, y, seq_len], [np.zeros((X.shape[0],)), y],
              batch_size=args.batch_size, nb_epoch=args.nb_epoch,
              validation_data=([X_valid, y_valid, seq_len_valid],
                               [np.zeros((X_valid.shape[0],)), y_valid]),
              callbacks=callback_list, shuffle=True)

    # history = model.fit([X, y, seq_len], np.zeros((X.shape[0],)),
    #                     batch_size=args.batch_size, nb_epoch=args.nb_epoch,
    #                     validation_data=([X_valid, y_valid, seq_len_valid],
    #                                      np.zeros((X_valid.shape[0],))))


    # meta = {'history': history.history, 'params': vars(args)}