# coding: utf-8
#
# Usage:
#   python examples/bidirectional_attention_multi_layer_nmt.py -m train -c configs/bidirectional_attention_multi_layer_nmt.ini
#   python examples/bidirectional_attention_multi_layer_nmt.py -m eval -c configs/bidirectional_attention_multi_layer_nmt.ini
#
# Purpose:
#   Input some sequence, then predict same sequence(+ EOS token).

import argparse
import os
import pickle
import pathlib
import shutil
import sys

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf

from configs.configs import Configs
from data.data import read_data, read_words, batchnize, build_dictionary, sentence_to_onehot, seq2seq
from utils.early_stopping import EarlyStopper
from utils.monitor import Monitor
from utils.logger import Logger


def main(args):
  # process config
  c = Configs(args.config)
  ROOT = os.environ['TENSOROFLOW']
  output = c.option.get('output', 'examples/model/buf')
  model_directory = '%s/%s' % (ROOT, output)
  model_path = '%s/model' % model_directory
  dictionary_path = {'source': '%s/source_dictionary.pickle' % model_directory,
                     'source_reverse': '%s/source_reverse_dictionary.pickle' % model_directory,
                     'target': '%s/target_dictionary.pickle' % model_directory,
                     'target_reverse': '%s/target_reverse_dictionary.pickle' % model_directory }
  PAD = c.const['PAD']
  BOS = c.const['BOS']
  EOS = c.const['EOS']
  train_step = c.option['train_step']
  max_time = c.option['max_time']
  batch_size = c.option['batch_size']
  vocabulary_size = c.option['vocabulary_size']
  input_embedding_size = c.option['embedding_size']
  hidden_units = c.option['hidden_units']
  layers = c.option['layers']
  source_train_data_path = c.data['source_train_data']
  target_train_data_path = c.data['target_train_data']
  source_valid_data_path = c.data['source_valid_data']
  target_valid_data_path = c.data['target_valid_data']
  source_test_data_path = c.data['source_test_data']
  target_test_data_path = c.data['target_test_data']

  # initialize output directory
  if pathlib.Path(model_directory).exists():
    print('Warning: model %s is exists.')
    print('Old model will be overwritten.')
    while True:
      print('Do you wanna continue? [yes|no]')
      command = input('> ')
      if command == 'yes':
        shutil.rmtree(model_directory)
        break
      elif command == 'no':
        sys.exit()
      else:
        print('You can only input "yes" or "no".')

  print('Make new model: %s' % model_directory)
  pathlib.Path(model_directory).mkdir()

  # read data
  if args.mode == 'train':
    source_dictionary, source_reverse_dictionary = build_dictionary(read_words(source_train_data_path), vocabulary_size)
    source_train_datas = [sentence_to_onehot(lines, source_dictionary) for lines in read_data(source_train_data_path)]
    target_dictionary, target_reverse_dictionary = build_dictionary(read_words(target_train_data_path), vocabulary_size)
    target_train_datas = [sentence_to_onehot(lines, target_dictionary) for lines in read_data(target_train_data_path)]

    source_valid_datas = [sentence_to_onehot(lines, source_dictionary) for lines in read_data(source_valid_data_path)]
    target_valid_datas = [sentence_to_onehot(lines, target_dictionary) for lines in read_data(target_valid_data_path)]

    if args.debug:
      source_train_datas = source_train_datas[:1000]
      target_train_datas = source_train_datas[:1000]
  else:
    with open(dictionary_path['source'], 'rb') as f1, \
         open(dictionary_path['source_reverse'], 'rb') as f2, \
         open(dictionary_path['target'], 'rb') as f3, \
         open(dictionary_path['target_reverse'], 'rb') as f4:
      source_dictionary = pickle.load(f1)
      source_reverse_dictionary = pickle.load(f2)
      target_dictionary = pickle.load(f3)
      target_reverse_dictionary = pickle.load(f4)

  source_test_datas = [sentence_to_onehot(lines, source_dictionary) for lines in read_data(source_test_data_path)]
  target_test_datas = [sentence_to_onehot(lines, target_dictionary) for lines in read_data(target_test_data_path)]

  # placeholder
  encoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name='encoder_inputs')
  decoder_inputs = tf.placeholder(shape=(None, None), dtype=tf.int32, name='decoder_inputs')
  decoder_labels = tf.placeholder(shape=(None, None), dtype=tf.int32, name='decoder_labels')

  # embed
  embeddings = tf.Variable(tf.random_uniform([vocabulary_size, input_embedding_size], -1.0, 1.0), dtype=tf.float32, name='embeddings')
  encoder_inputs_embedded = tf.nn.embedding_lookup(embeddings, encoder_inputs)
  decoder_inputs_embedded = tf.nn.embedding_lookup(embeddings, decoder_inputs)

  # encoder with bidirection
  encoder_units = hidden_units
  encoder_layers_fw = [tf.contrib.rnn.LSTMCell(size) for size in [encoder_units] * layers]
  encoder_cell_fw = tf.contrib.rnn.MultiRNNCell(encoder_layers_fw)
  encoder_layers_bw = [tf.contrib.rnn.LSTMCell(size) for size in [encoder_units] * layers]
  encoder_cell_bw = tf.contrib.rnn.MultiRNNCell(encoder_layers_bw)
  (encoder_output_fw, encoder_output_bw), encoder_state = tf.nn.bidirectional_dynamic_rnn(
      encoder_cell_fw, encoder_cell_bw, encoder_inputs_embedded,
      dtype=tf.float32, time_major=True
  )
  encoder_outputs = tf.concat((encoder_output_fw, encoder_output_bw), 2)
  encoder_state = tuple(tf.contrib.rnn.LSTMStateTuple(tf.concat((encoder_state[0][layer].c, encoder_state[1][layer].c), 1), tf.concat((encoder_state[0][layer].h, encoder_state[1][layer].h), 1)) for layer in range(layers)) 

  # decoder with attention
  decoder_units = encoder_units * 2
  attention_units = decoder_units
  decoder_layers = [tf.contrib.rnn.LSTMCell(size) for size in [decoder_units] * layers]
  cell = tf.contrib.rnn.MultiRNNCell(decoder_layers)

  sequence_length = tf.cast([max_time] * batch_size, dtype=tf.int32)
  beam_width = 1
  tiled_encoder_outputs = tf.contrib.seq2seq.tile_batch(
      encoder_outputs, multiplier=beam_width)
  tiled_encoder_final_state = tf.contrib.seq2seq.tile_batch(
      encoder_state, multiplier=beam_width)
  tiled_sequence_length = tf.contrib.seq2seq.tile_batch(
      sequence_length, multiplier=beam_width)
  attention_mechanism = tf.contrib.seq2seq.LuongAttention(
      num_units=attention_units,
      memory=tiled_encoder_outputs,
      memory_sequence_length=tiled_sequence_length)
  attention_cell = tf.contrib.seq2seq.AttentionWrapper(
    cell, attention_mechanism, attention_layer_size=256)
  decoder_initial_state = attention_cell.zero_state(
      dtype=tf.float32, batch_size=batch_size * beam_width)
  decoder_initial_state = decoder_initial_state.clone(
      cell_state=tiled_encoder_final_state)

  if args.mode == 'train':
    helper = tf.contrib.seq2seq.TrainingHelper(
      inputs=decoder_inputs_embedded,
      sequence_length=tf.cast([max_time] * batch_size, dtype=tf.int32),
      time_major=True)
  elif args.mode == 'eval':
    """
    helper = tf.contrib.seq2seq.TrainingHelper(
      inputs=decoder_inputs_embedded,
      sequence_length=tf.cast([max_time] * batch_size, dtype=tf.int32),
      time_major=True)
    """
    helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
      embedding=embeddings,
      start_tokens=tf.tile([BOS], [batch_size]),
      end_token=EOS) 

  decoder = tf.contrib.seq2seq.BasicDecoder(
    cell=attention_cell,
    helper=helper,
    initial_state=decoder_initial_state)
  decoder_outputs = tf.contrib.seq2seq.dynamic_decode(
     decoder=decoder,
     output_time_major=True,
     impute_finished=False,
     maximum_iterations=max_time)

  decoder_logits = tf.contrib.layers.linear(decoder_outputs[0][0], vocabulary_size)
  decoder_prediction = tf.argmax(decoder_logits, 2) # max_time: axis=0, batch: axis=1, vocab: axis=2

  # optimizer
  stepwise_cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
      labels=tf.one_hot(decoder_labels, depth=vocabulary_size, dtype=tf.float32),
      logits=decoder_logits,
  )

  loss = tf.reduce_mean(stepwise_cross_entropy)
  regularizer = 0.0 * tf.nn.l2_loss(decoder_outputs[0][0])
  train_op = tf.train.AdamOptimizer().minimize(loss + regularizer)
  
  saver = tf.train.Saver()
  minibatch_idx = {'train': 0, 'valid': 0, 'test': 0}
  with tf.Session() as sess:
    if args.mode == 'train':
      # train
      global_max_step = train_step * (len(source_train_datas) // batch_size + 1)
      loss_freq = global_max_step // 100 if global_max_step > 100 else 1
      loss_log = []
      batch_loss_log = []
      loss_suffix = ''
      es = EarlyStopper(max_size=5, edge_threshold=0.1)
      m = Monitor(global_max_step)
      log = Logger('%s/log' % model_directory)
      sess.run(tf.global_variables_initializer())
      global_step = 0
      stop_flag = False
      for batch in range(train_step):
        if stop_flag:
          break
        current_batch_loss_log = []
        while True: # minibatch process
          m.monitor(global_step, loss_suffix)
          source_train_batch, _ = batchnize(source_train_datas, batch_size, minibatch_idx['train'])
          target_train_batch, minibatch_idx['train'] = batchnize(target_train_datas, batch_size, minibatch_idx['train'])
          batch_data = seq2seq(source_train_batch, target_train_batch, max_time, vocabulary_size, reverse=True)
          feed_dict = {encoder_inputs:batch_data['encoder_inputs'],
                       decoder_inputs:batch_data['decoder_inputs'],
                       decoder_labels:batch_data['decoder_labels']}
          sess.run(fetches=[train_op, loss], feed_dict=feed_dict)

          if global_step % loss_freq == 0:
            source_valid_batch, _ = batchnize(source_valid_datas, batch_size, minibatch_idx['valid'])
            target_valid_batch, minibatch_idx['valid'] = batchnize(target_valid_datas, batch_size, minibatch_idx['valid'])
            batch_data = seq2seq(source_valid_batch, target_valid_batch, max_time, vocabulary_size, reverse=True)
            feed_dict = {encoder_inputs:batch_data['encoder_inputs'],
                         decoder_inputs:batch_data['decoder_inputs'],
                         decoder_labels:batch_data['decoder_labels']}
            loss_val = sess.run(fetches=loss, feed_dict=feed_dict)
            loss_log.append(loss_val)
            current_batch_loss_log.append(loss_val)
            loss_suffix = 'loss: %f' % loss_val
          global_step += 1
          if minibatch_idx['train'] == 0:
            batch_loss = np.mean(current_batch_loss_log)
            batch_loss_log.append(batch_loss)
            loss_msg = 'Batch: {}/{}, batch loss: {}'.format(batch + 1, train_step, batch_loss)
            print(loss_msg)
            log(loss_msg)
            es_status = es(batch_loss)
            if batch > train_step // 2 and es_status:
              print('early stopping at step: %d' % global_step)
              stop_flag = True
            break

      # save tf.graph and variables
      saver.save(sess, model_path)
      print('save at %s' % model_path)

      # save plot of loss
      plt.plot(np.arange(len(loss_log)) * loss_freq, loss_log)
      plt.savefig('%s_global_loss.png' % model_path)
      plt.figure()
      plt.plot(np.arange(len(batch_loss_log)), batch_loss_log)
      plt.savefig('%s_batch_loss.png' % model_path)

      # save dictionary
      with open(dictionary_path['source'], 'wb') as f1, \
           open(dictionary_path['source_reverse'], 'wb') as f2, \
           open(dictionary_path['target'], 'wb') as f3, \
           open(dictionary_path['target_reverse'], 'wb') as f4:
        pickle.dump(source_dictionary, f1)
        pickle.dump(source_reverse_dictionary, f2)
        pickle.dump(target_dictionary, f3)
        pickle.dump(target_reverse_dictionary, f4)

    elif args.mode == 'eval':
      saver.restore(sess, model_path)
      print('load from %s' % model_path)

    else:
      raise # args.mode should be train or eval

    # evaluate
    loss_val = []
    input_vectors = None
    predict_vectors = None
    for i in range(len(source_test_datas) // batch_size + 1):
      source_test_batch, _ = batchnize(source_test_datas, batch_size, minibatch_idx['test'])
      target_test_batch, minibatch_idx['test'] = batchnize(target_test_datas, batch_size, minibatch_idx['test'])
      batch_data = seq2seq(source_test_batch, target_test_batch, max_time, vocabulary_size, reverse=True)
      feed_dict = {encoder_inputs:batch_data['encoder_inputs'],
                   decoder_inputs:batch_data['decoder_inputs'],
                   decoder_labels:batch_data['decoder_labels']}
      pred = sess.run(fetches=decoder_prediction, feed_dict=feed_dict)
      if predict_vectors is None:
        predict_vectors = pred.T
      else:
        predict_vectors = np.vstack((predict_vectors, pred.T))
      input_ = batch_data['encoder_inputs']
      if input_vectors is None:
        input_vectors = input_.T
      else:
        input_vectors = np.vstack((input_vectors, input_.T))
      loss_val.append(sess.run(fetches=loss, feed_dict=feed_dict))

    input_sentences = ''
    predict_sentences = ''
    ignore_token = EOS
    for i, (input_vector, predict_vector) in enumerate(zip(input_vectors[:len(source_test_datas)], predict_vectors[:len(target_test_datas)])):
      input_sentences += ' '.join([source_reverse_dictionary[vector] for vector in input_vector if not vector == ignore_token])
      predict_sentences += ' '.join([target_reverse_dictionary[vector] for vector in predict_vector if not vector == ignore_token])
      if i < len(source_test_datas) - 1:
        input_sentences += '\n'
        predict_sentences += '\n'

    evaluate_input_path = '%s.evaluate_input' % model_path
    evaluate_predict_path = '%s.evaluate_predict' % model_path
    with open(evaluate_input_path, 'w') as f1, \
         open(evaluate_predict_path, 'w') as f2:
      f1.write(input_sentences)
      f2.write(predict_sentences)

    print('input sequences at {}'.format(evaluate_input_path))
    print('predict sequences at {}'.format(evaluate_predict_path))
    print('mean of loss: %f' % np.mean(loss_val))

  print('finish.')

if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('--mode', '-m', type=str, help='train | eval')
  parser.add_argument('--config', '-c', type=str, help='config file path')
  parser.add_argument('--debug', '-d', type=bool, default=False, help='flag of debug mode')
  args = parser.parse_args()
  main(args)
  
