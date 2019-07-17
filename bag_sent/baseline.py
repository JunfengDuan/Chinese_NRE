import numpy as np
import tensorflow as tf
import random
import os
import json
import datetime
from collections import Counter
from bert_serving.client import BertClient


def set_seed():
    os.environ['PYTHONHASHSEED'] = '0'
    np.random.seed(2019)
    random.seed(2019)
    tf.compat.v1.set_random_seed(2019)
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
set_seed()

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_string('cuda', '0', 'gpu id')
tf.app.flags.DEFINE_boolean('pre_embed', True, 'load pre-trained word2vec')
tf.app.flags.DEFINE_boolean('embed_bert', False, 'using bert to sentence2vec ')
tf.app.flags.DEFINE_integer('batch_size', 128, 'batch size')
tf.app.flags.DEFINE_integer('epochs', 25, 'max train epochs')
tf.app.flags.DEFINE_integer('hidden_dim', 300, 'dimension of hidden embedding')
tf.app.flags.DEFINE_integer('word_dim', 300, 'dimension of word embedding')
tf.app.flags.DEFINE_integer('pos_dim', 5, 'dimension of position embedding')
tf.app.flags.DEFINE_integer('pos_limit', 15, 'max distance of position embedding')
tf.app.flags.DEFINE_integer('sen_len', 60, 'sentence length')
tf.app.flags.DEFINE_integer('window', 3, 'window size')
tf.app.flags.DEFINE_string('model_path', './model', 'save model dir')
tf.app.flags.DEFINE_string('data_path', './data', 'data dir to load')
tf.app.flags.DEFINE_string('level', 'bag', 'bag level or sentence level, option:bag/sent')
tf.app.flags.DEFINE_string('encoder', 'cnn', 'encoder,option:cnn/pcnn/bi_rnn')
tf.app.flags.DEFINE_string('mode', 'train', 'train or test')
tf.app.flags.DEFINE_float('dropout', 0.5, 'dropout rate')
tf.app.flags.DEFINE_float('bag_threshold', 0.9, 'option:(0,1)')
tf.app.flags.DEFINE_float('lr', 0.001, 'learning rate')
tf.app.flags.DEFINE_integer('word_frequency', 5, 'minimum word frequency when constructing vocabulary list')


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        else:
            return super(NpEncoder, self).default(obj)


class Baseline:
    def __init__(self, flags):
        self.lr = flags.lr
        self.sen_len = flags.sen_len
        self.pre_embed = flags.pre_embed
        self.pos_limit = flags.pos_limit
        self.pos_dim = flags.pos_dim
        self.window = flags.window
        self.word_dim = flags.word_dim
        self.hidden_dim = flags.hidden_dim
        self.batch_size = flags.batch_size
        self.data_path = flags.data_path
        self.model_path = flags.model_path
        self.encoder = flags.encoder
        self.mode = flags.mode
        self.epochs = flags.epochs
        self.dropout = flags.dropout
        self.bag_threshold = flags.bag_threshold
        self.word_frequency = flags.word_frequency
        if flags.level == 'sent':
            self.bag = False
        elif flags.level == 'bag':
            self.bag = True
        else:
            self.bag = True
       
        if flags.embed_bert:
            self.embed_bert =True
            self.word_dim = 768
            self.bert = BertClient(ip='localhost',check_version=False, check_length=False)
        else:
            self.embed_bert = False
            
        self.relation2id = self.load_relation()
        self.num_classes = len(self.relation2id)
            
        self.pos_num = 2 * self.pos_limit + 3
        if self.pre_embed:
            self.wordMap, word_embed = self.load_wordVec()
            self.word_embedding = tf.compat.v1.get_variable(initializer=word_embed, name='word_embedding', trainable=False)

        elif  self.embed_bert and self.mode=='train':
            self.wordMap, word_embed = self.bert_wordMap()
            self.word_embedding = tf.compat.v1.get_variable(initializer=word_embed, name='word_embedding', trainable=False)
        elif  self.embed_bert and self.mode=='test':
            self.wordMap, word_embed = self.load_bert_word2vec()
            self.word_embedding = tf.compat.v1.get_variable(initializer=word_embed, name='word_embedding', trainable=False)
        else:
            self.wordMap = self.load_wordMap()
            self.word_embedding = tf.compat.v1.get_variable(shape=[len(self.wordMap), self.word_dim], name='word_embedding',trainable=True)

        self.pos_e1_embedding = tf.compat.v1.get_variable(name='pos_e1_embedding', shape=[self.pos_num, self.pos_dim])
        self.pos_e2_embedding = tf.compat.v1.get_variable(name='pos_e2_embedding', shape=[self.pos_num, self.pos_dim])
        if self.encoder=='pcnn':
            self.relation_embedding = tf.compat.v1.get_variable(name='relation_embedding', shape=[self.hidden_dim*3, self.num_classes])
        else:
            self.relation_embedding = tf.compat.v1.get_variable(name='relation_embedding', shape=[self.hidden_dim, self.num_classes])
        self.relation_embedding_b = tf.compat.v1.get_variable(name='relation_embedding_b', shape=[self.num_classes])
        
        if self.encoder=='cnn':
            self.sentence_reps = self.cnn()
        elif self.encoder=='pcnn':
            self.sentence_reps = self.pcnn()
        elif self.encoder=='rnn':
            self.sentence_reps = self.rnn()
        else:
            self.sentence_reps = self.bi_rnn()
                
        if self.bag:
            self.bag_level()
        else:
            self.sentence_level()
        self._classifier_train_op = tf.compat.v1.train.AdamOptimizer(self.lr).minimize(self.classifier_loss)

    def pos_index(self, x):
        if x < -self.pos_limit:
            return 0
        if x >= -self.pos_limit and x <= self.pos_limit:
            return x + self.pos_limit + 1
        if x > self.pos_limit:
            return 2 * self.pos_limit + 2

    def load_wordVec(self):
        wordMap = {}
        wordMap['PAD'] = len(wordMap)
        wordMap['UNK'] = len(wordMap)
        word_embed = []
        for line in open(os.path.join(self.data_path, 'word2vec.txt')):
            content = line.strip().split()
            if len(content) != self.word_dim + 1:
                continue
            wordMap[content[0]] = len(wordMap)
            word_embed.append(np.asarray(content[1:], dtype=np.float32))

        word_embed = np.stack(word_embed)
        embed_mean, embed_std = word_embed.mean(), word_embed.std()

        pad_embed = np.random.normal(embed_mean, embed_std, (2, self.word_dim))
        word_embed = np.concatenate((pad_embed, word_embed), axis=0)
        word_embed = word_embed.astype(np.float32)
        return wordMap, word_embed

    def load_bert_word2vec(self):
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('加载bert sentence2vec'))
        print(tempstr)
        ori_word_vec =  json.load(open(os.path.join(self.data_path, 'bert_word2vec.json'), "r"))
        word_embed = np.zeros((len(ori_word_vec), self.word_dim), dtype=np.float32)
        wordMap = {}
        for cur_id, word in enumerate(ori_word_vec):
            w = word['word']
            wordMap[w] = cur_id
            word_embed[cur_id, :] = word['vec']
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('加载bert sentence2vec完成'))
        print(tempstr)
        return wordMap,word_embed
    
    def load_wordMap(self):
        wordMap = {}
        wordMap['PAD'] = len(wordMap)
        wordMap['UNK'] = len(wordMap)
        all_content = []
        for line in open(os.path.join(self.data_path, 'sent_train.txt')):
            all_content += line.strip().split('\t')[3].split()
        for item in Counter(all_content).most_common():
            if item[1] > self.word_frequency:
                wordMap[item[0]] = len(wordMap)
            else:
                break
        return wordMap
    
    
    def bert_wordMap(self):
        wordMap = {}
        all_content = []
        all_content.append('PAD')
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('加载语料库'))
        print(tempstr)
        for line in open(os.path.join(self.data_path, 'sent_train.txt')):
            all_content += line.strip().split('\t')[3].split()
        all_content = list(set(all_content)) 
        for line in open(os.path.join(self.data_path, 'sent_test.txt')):
            all_content += line.strip().split('\t')[3].split()
        all_content = list(set(all_content))
        for line in open(os.path.join(self.data_path, 'sent_dev.txt')):
            all_content += line.strip().split('\t')[3].split()
        all_content = list(set(all_content))
        wordMap = dict(zip(all_content,range(len(all_content))))
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('语料库加载完成，提取词向量中,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,,'))
        print(tempstr)
        word_embed = self.bert.encode(all_content)
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('提取词向量完成'))
        print(tempstr)
        
        
        #保存好提取的bert模型的word2vec
        #形式是：[{"word": "我的", "vec": [1, 2, 3]}, {"word": "中国", "vec": [4, 5, 6]}, {"word": "使得", "vec": [2, 4, 5]}]
        print('保存bert word2vec 到json文件中')
        word2vec_list = []
        for word,vec in zip(all_content,word_embed):
            word2vec_dict = {}
            word2vec_dict['word'] = word
            word2vec_dict['vec'] = vec
            word2vec_list.append(word2vec_dict)
        filew = open (os.path.join(self.data_path, 'bert_word2vec.json'), 'w', encoding='utf-8')
        json.dump(word2vec_list,filew,cls=NpEncoder,ensure_ascii=False)
        filew.close()
        time_str = datetime.datetime.now().isoformat()
        tempstr = "{}:{}".format(time_str,str('保存完成'))
        print(tempstr)
        
        word_embed = np.array(bert.encode(all_content),np.float32)
        return wordMap,word_embed


    
    def load_relation(self):
        relation2id = {}
        for line in open(os.path.join(self.data_path, 'relation2id.txt')):
            relation, id_ = line.strip().split()
            relation2id[relation] = int(id_)
        return relation2id
    
    def load_sent(self, filename):
        sentence_dict = {}
        with open(os.path.join(self.data_path, filename), 'r') as fr:
            for line in fr:
                id_, en1, en2, sentence = line.strip().split('\t')
                sentence = sentence.split()
                en1_pos = 0
                en2_pos = 0
                for i in range(len(sentence)):
                    if sentence[i] == en1:
                        en1_pos = i
                    if sentence[i] == en2:
                        en2_pos = i
                words = []
                pos1 = []
                pos2 = []
                mask = []
                length = min(self.sen_len, len(sentence))
                pos_min = min(en1_pos, en2_pos)
                pos_max = max(en1_pos, en2_pos)
                
                for i in range(length):
                    if self.embed_bert:
                        words.append(self.wordMap.get(sentence[i]))
                    else:
                        words.append(self.wordMap.get(sentence[i], self.wordMap['UNK']))
                    pos1.append(self.pos_index(i - en1_pos))
                    pos2.append(self.pos_index(i - en2_pos))
                    if i<=pos_min:
                        mask.append(1)
                    elif i<=pos_max:
                        mask.append(2)
                    else:
                        mask.append(3)

                if length < self.sen_len:
                    for i in range(length, self.sen_len):
                        words.append(self.wordMap['PAD'])
                        pos1.append(self.pos_index(i - en1_pos))
                        pos2.append(self.pos_index(i - en2_pos))
                        mask.append(0)
                sentence_dict[id_] = np.reshape(np.asarray([words, pos1, pos2,mask],dtype=np.int32), (1, 4, self.sen_len))
        return sentence_dict


    def data_batcher(self, sentence_dict, filename, padding=False, shuffle=True):
        if self.bag:
            all_bags = []
            all_sents = []
            all_labels = []
            with open(os.path.join(self.data_path, filename), 'r') as fr:
                for line in fr:
                    rel = [0] * self.num_classes
                    try:
                        bag_id, _, _, sents, types = line.strip().split('\t')
                        type_list = types.split()
                        for tp in type_list:
                            if len(type_list) > 1 and tp == '0': # if a bag has multiple relations, we only consider non-NA relations
                                continue
                            rel[int(tp)] = 1
                    except:
                        bag_id, _, _, sents = line.strip().split('\t')

                    sent_list = []
                    for sent in sents.split():
                        sent_list.append(sentence_dict[sent])

                    all_bags.append(bag_id)
                    all_sents.append(np.concatenate(sent_list,axis=0))
                    all_labels.append(np.asarray(rel, dtype=np.float32))

            self.data_size = len(all_bags)
            self.datas = all_bags
            data_order = list(range(self.data_size))
            
            if shuffle:
                np.random.shuffle(data_order)
            if padding:
                if self.data_size % self.batch_size != 0:
                    data_order += [data_order[-1]] * (self.batch_size - self.data_size % self.batch_size)
            for i in range(len(data_order) // self.batch_size):
                total_sens = 0
                out_sents = []
                out_sent_nums = []
                out_labels = []
                for k in data_order[i * self.batch_size:(i + 1) * self.batch_size]:
                    out_sents.append(all_sents[k])
                    out_sent_nums.append(total_sens)
                    total_sens += all_sents[k].shape[0]
                    out_labels.append(all_labels[k])


                out_sents = np.concatenate(out_sents, axis=0)
                out_sent_nums.append(total_sens)
                out_sent_nums = np.asarray(out_sent_nums, dtype=np.int32)
                out_labels = np.stack(out_labels)

                yield out_sents, out_labels, out_sent_nums
        else:
            all_sent_ids = []
            all_sents = []
            all_labels = []
            with open(os.path.join(self.data_path, filename), 'r') as fr:
                for line in fr:
                    rel = [0] * self.num_classes
                    try:
                        sent_id, types = line.strip().split('\t')
                        type_list = types.split()
                        for tp in type_list:
                            if len(type_list) > 1 and tp == '0': # if a sentence has multiple relations, we only consider non-NA relations
                                continue
                            rel[int(tp)] = 1
                    except:
                        sent_id = line.strip()

                    all_sent_ids.append(sent_id)
                    all_sents.append(sentence_dict[sent_id])

                    all_labels.append(np.reshape(np.asarray(rel, dtype=np.float32), (-1, self.num_classes)))

            self.data_size = len(all_sent_ids)
            self.datas = all_sent_ids

            all_sents = np.concatenate(all_sents, axis=0)
            all_labels = np.concatenate(all_labels, axis=0)

            data_order = list(range(self.data_size))
            if shuffle:
                np.random.shuffle(data_order)
            if padding:
                if self.data_size % self.batch_size != 0:
                    data_order += [data_order[-1]] * (self.batch_size - self.data_size % self.batch_size)

            for i in range(len(data_order) // self.batch_size):
                idx = data_order[i * self.batch_size:(i + 1) * self.batch_size]
                yield all_sents[idx], all_labels[idx], None
    def embedding(self):
        self.keep_prob = tf.compat.v1.placeholder(dtype=tf.float32, name='keep_prob')
        self.input_word = tf.compat.v1.placeholder(dtype=tf.int32, shape=[None, self.sen_len], name='input_word')
        self.input_pos_e1 = tf.compat.v1.placeholder(dtype=tf.int32, shape=[None, self.sen_len], name='input_pos_e1')
        self.input_pos_e2 = tf.compat.v1.placeholder(dtype=tf.int32, shape=[None, self.sen_len], name='input_pos_e2')
        if self.encoder=='pcnn':
            self.mask = tf.compat.v1.placeholder(dtype=tf.int32, shape=[None, self.sen_len], name='mask')
        self.input_label = tf.compat.v1.placeholder(dtype=tf.float32, shape=[None, self.num_classes], name='input_label')

        inputs_forward = tf.concat(axis=2, values=[tf.nn.embedding_lookup(self.word_embedding, self.input_word), \
                                                   tf.nn.embedding_lookup(self.pos_e1_embedding, self.input_pos_e1), \
                                                   tf.nn.embedding_lookup(self.pos_e2_embedding, self.input_pos_e2)])
        return inputs_forward
    
    def cnn(self):
        #[batch_size,self.sen_len,self.word_dim+2*self.pos_dim]:bag时 为[377,60,310]
        inputs_forward = self.embedding()
        #[batch_size,self.sen_len,self.word_dim+2*self.pos_dim,1]
        inputs_forward = tf.expand_dims(inputs_forward, -1)
        with tf.compat.v1.name_scope('conv-maxpool'):
            w = tf.compat.v1.get_variable(name='w', shape=[self.window, self.word_dim + 2 * self.pos_dim, 1, self.hidden_dim])
            b = tf.compat.v1.get_variable(name='b', shape=[self.hidden_dim])
            conv = tf.nn.conv2d(
                inputs_forward,
                w,
                strides=[1, 1, 1, 1],
                padding='VALID',
                name='conv')
            #[batch_size,self.sen_len-self.window+1,1,self.hidden_dim]
            h = tf.nn.bias_add(conv, b)
            pooled = tf.nn.max_pool(
                h,
                ksize=[1, self.sen_len - self.window + 1, 1, 1],
                strides=[1, 1, 1, 1],
                padding='VALID',
                name='pool')
        sen_reps = tf.tanh(tf.reshape(pooled, [-1, self.hidden_dim]))
        sen_reps = tf.nn.dropout(sen_reps, self.keep_prob)
        return sen_reps
    
    def pcnn(self):
        mask_embedding = tf.constant([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=np.float32)
        #[batch_size,self.sen_len,self.word_dim+2*self.pos_dim]:bag时 为[377,60,310]
        inputs_forward = self.embedding()
        #[batch_size,self.sen_len,self.hidden_dim]
        conv = tf.layers.conv1d(inputs=inputs_forward, 
                         filters=self.hidden_dim, 
                         kernel_size=3, 
                         strides=1, 
                         padding='same', 
                         kernel_initializer=tf.contrib.layers.xavier_initializer())
        
        #self.mask:[batch_size,self.sen_len] mask:[batch_size,self.sen_len,3]
        mask = tf.nn.embedding_lookup(mask_embedding, self.mask)
        #tf.expand_dims(mask * 100, 2):[batch_size,self.sen_len,1,3] tf.expand_dims(conv, 3):[batch_size,self.sen_len,self.hidden_dim,1]
        #sen_reps:[batch_size,self.hidden_dim,3]
        sen_reps = tf.reduce_max(tf.expand_dims(mask * 100, 2) + tf.expand_dims(conv, 3), axis=1) - 100
        sen_reps = tf.tanh(tf.reshape(sen_reps, [-1, self.hidden_dim * 3]))
        sen_reps = tf.nn.dropout(sen_reps, self.keep_prob)
        return sen_reps
    
    
    def rnn_cell(self,cell_name='lstm'):
        if isinstance(cell_name, list) or isinstance(cell_name, tuple):
            if len(cell_name) == 1:
                return self.rnn_cell(cell_name[0])
            cells = [self.rnn_cell(c) for c in cell_name]
            return tf.contrib.rnn.MultiRNNCell(cells, state_is_tuple=True)
        if cell_name.lower() == 'lstm':
            return tf.contrib.rnn.BasicLSTMCell(self.hidden_dim, state_is_tuple=True)
        elif cell_name.lower() == 'gru':
            return tf.contrib.rnn.GRUCell(self.hidden_dim)
        raise NotImplementedError
    
    def rnn(self,cell_name='lstm'):
        inputs_forward = self.embedding()
        inputs_forward = tf.nn.dropout(inputs_forward, self.keep_prob)
        cell = self.rnn_cell(cell_name)
        _, states = tf.nn.dynamic_rnn(cell, inputs_forward, sequence_length=[self.sen_len]*self.batch_size, dtype=tf.float32, scope='dynamic-rnn')
        if isinstance(states, tuple):
            states = states[0]
        return states
    
    
    def bi_rnn(self,cell_name='lstm'):
        inputs_forward = self.embedding()
       
        inputs_forward = tf.nn.dropout(inputs_forward, keep_prob=self.dropout)
        fw_cell = self.rnn_cell(cell_name)
        bw_cell = self.rnn_cell(cell_name)
        _, states = tf.nn.bidirectional_dynamic_rnn(fw_cell, bw_cell,inputs_forward, dtype=tf.float32, scope='dynamic-bi-rnn')
        fw_states, bw_states = states
        if isinstance(fw_states, tuple):
            fw_states = fw_states[0]
            bw_states = bw_states[0]
        sen_reps = tf.tanh(tf.reshape(tf.concat([fw_states, bw_states], axis=1), [-1, self.hidden_dim]))
        sen_reps = tf.nn.dropout(sen_reps, self.keep_prob)
        return sen_reps
    

    def bag_level(self):
        self.classifier_loss = 0.0
        self.probability = []
        
        if self.encoder=='pcnn':
            hidden_dim_cur = self.hidden_dim*3
        else:
            hidden_dim_cur = self.hidden_dim
        
        self.bag_sens = tf.compat.v1.placeholder(dtype=tf.int32, shape=[self.batch_size + 1], name='bag_sens')
        self.att_A = tf.compat.v1.get_variable(name='att_A', shape=[hidden_dim_cur])
        self.rel = tf.reshape(tf.transpose(self.relation_embedding), [self.num_classes, hidden_dim_cur])
        for i in range(self.batch_size):
            sen_reps = tf.reshape(self.sentence_reps[self.bag_sens[i]:self.bag_sens[i + 1]], [-1, hidden_dim_cur])
            
            att_sen = tf.reshape(tf.multiply(sen_reps, self.att_A), [-1, hidden_dim_cur])
            score = tf.matmul(self.rel, tf.transpose(att_sen))
            alpha = tf.nn.softmax(score, 1)
            bag_rep = tf.matmul(alpha, sen_reps) 
            out = tf.matmul(bag_rep, self.relation_embedding) + self.relation_embedding_b

            prob = tf.reshape(tf.reduce_sum(tf.nn.softmax(out, 1) * tf.reshape(self.input_label[i], [-1, 1]), 0),
                              [self.num_classes])

            self.probability.append(
                tf.reshape(tf.reduce_sum(tf.nn.softmax(out, 1) * tf.linalg.tensor_diag([1.0] * (self.num_classes)), 1),
                           [-1, self.num_classes]))
            self.classifier_loss += tf.reduce_sum(
                -tf.log(tf.clip_by_value(prob, 1.0e-10, 1.0)) * tf.reshape(self.input_label[i], [-1]))
        
        self.probability = tf.concat(axis=0, values=self.probability)
        self.classifier_loss = self.classifier_loss / tf.cast(self.batch_size, tf.float32)              
                
    def sentence_level(self):
        
        out = tf.matmul(self.sentence_reps, self.relation_embedding) + self.relation_embedding_b
        self.probability = tf.nn.softmax(out, 1)
        self.classifier_loss = tf.reduce_mean(
            tf.reduce_sum(-tf.log(tf.clip_by_value(self.probability, 1.0e-10, 1.0)) * self.input_label, 1))

    def run_train(self, sess, batch):

        sent_batch, label_batch, sen_num_batch = batch

        feed_dict = {}
        feed_dict[self.keep_prob] = 1-self.dropout
        feed_dict[self.input_word] = sent_batch[:, 0, :]
        feed_dict[self.input_pos_e1] = sent_batch[:, 1, :]
        feed_dict[self.input_pos_e2] = sent_batch[:, 2, :]
        if self.encoder=='pcnn':
            feed_dict[self.mask] = sent_batch[:, 3, :]
        feed_dict[self.input_label] = label_batch
        if self.bag:
            feed_dict[self.bag_sens] = sen_num_batch
        _, classifier_loss = sess.run([self._classifier_train_op, self.classifier_loss], feed_dict)
        return classifier_loss


    def run_dev(self, sess, dev_batchers):
        all_labels = []
        all_probs = []
        for batch in dev_batchers:
            sent_batch, label_batch, sen_num_batch = batch
            all_labels.append(label_batch)

            feed_dict = {}
            feed_dict[self.keep_prob] = 1.0
            feed_dict[self.input_word] = sent_batch[:, 0, :]
            feed_dict[self.input_pos_e1] = sent_batch[:, 1, :]
            feed_dict[self.input_pos_e2] = sent_batch[:, 2, :]
            
            if self.encoder=='pcnn':
                feed_dict[self.mask] = sent_batch[:, 3, :]
            if self.bag:
                feed_dict[self.bag_sens] = sen_num_batch
            prob = sess.run([self.probability], feed_dict)
            all_probs.append(np.reshape(prob, (-1, self.num_classes)))

        all_labels = np.concatenate(all_labels, axis=0)[:self.data_size]
        all_probs = np.concatenate(all_probs, axis=0)[:self.data_size]
        if self.bag:
            all_preds = all_probs
            all_preds[all_probs > self.bag_threshold] = 1
            all_preds[all_probs <= self.bag_threshold] = 0
        else:
            all_preds = np.eye(self.num_classes)[np.reshape(np.argmax(all_probs, 1), (-1))]

        return all_preds, all_labels

    def run_test(self, sess, test_batchers):
        all_probs = []
        for batch in test_batchers:
            sent_batch, _, sen_num_batch = batch

            feed_dict = {}
            feed_dict[self.keep_prob] = 1.0
            feed_dict[self.input_word] = sent_batch[:, 0, :]
            feed_dict[self.input_pos_e1] = sent_batch[:, 1, :]
            feed_dict[self.input_pos_e2] = sent_batch[:, 2, :]
            if self.encoder=='pcnn':
                feed_dict[self.mask] = sent_batch[:, 3, :]
            if self.bag:
                feed_dict[self.bag_sens] = sen_num_batch
            prob = sess.run([self.probability], feed_dict)
            all_probs.append(np.reshape(prob, (-1, self.num_classes)))

        all_probs = np.concatenate(all_probs,axis=0)[:self.data_size]
        if self.bag:
            all_preds = all_probs
            all_preds[all_probs > self.bag_threshold] = 1
            all_preds[all_probs <= self.bag_threshold] = 0
        else:
            all_preds = np.eye(self.num_classes)[np.reshape(np.argmax(all_probs, 1), (-1))]

        if self.bag:
            with open('result_bag.txt', 'w') as fw:
                for i in range(self.data_size):
                    rel_one_hot = [int(num) for num in all_preds[i].tolist()]
                    rel_list = []
                    for j in range(0, self.num_classes):
                        if rel_one_hot[j] == 1:
                            rel_list.append(str(j))
                    if len(rel_list) == 0: # if a bag has no relation, it will be consider as having a relation NA
                        rel_list.append('0')
                    fw.write(self.datas[i] + '\t' + ' '.join(rel_list) + '\n')
        else:
            with open('result_sent.txt', 'w') as fw:
                for i in range(self.data_size):
                    rel_one_hot = [int(num) for num in all_preds[i].tolist()]
                    rel_list = []
                    for j in range(0, self.num_classes):
                        if rel_one_hot[j] == 1:
                            rel_list.append(str(j))
                    fw.write(self.datas[i] + '\t' + ' '.join(rel_list) + '\n')

    def run_model(self, sess, saver):
        if self.mode == 'train':
            global_step = 0
            sent_train = self.load_sent('sent_train.txt')
            sent_dev = self.load_sent('sent_dev.txt')
            max_f1 = 0.0

            if not os.path.isdir(self.model_path):
                os.mkdir(self.model_path)

            for epoch in range(self.epochs):
                if self.bag:
                    train_batchers = self.data_batcher(sent_train, 'bag_relation_train.txt', padding=False, shuffle=True)
                else:
                    train_batchers = self.data_batcher(sent_train, 'sent_relation_train.txt', padding=False, shuffle=True)
                for batch in train_batchers:

                    losses = self.run_train(sess, batch)
                    global_step += 1
                    if global_step % 50 == 0:
                        time_str = datetime.datetime.now().isoformat()
                        tempstr = "{}: step {}, classifier_loss {:g}".format(time_str, global_step, losses)
                        print(tempstr)
                    if global_step % 100 == 0:
                        if self.bag:
                            dev_batchers = self.data_batcher(sent_dev, 'bag_relation_dev.txt', padding=True, shuffle=False)
                        else:
                            dev_batchers = self.data_batcher(sent_dev, 'sent_relation_dev.txt', padding=True, shuffle=False)
                        all_preds, all_labels = self.run_dev(sess, dev_batchers)

                        # when calculate f1 score, we don't consider whether NA results are predicted or not
                        # the number of non-NA answers in test is counted as n_std
                        # the number of non-NA answers in predicted answers is counted as n_sys
                        # intersection of two answers is counted as n_r
                        n_r = int(np.sum(all_preds[:, 1:] * all_labels[:, 1:]))
                        n_std = int(np.sum(all_labels[:,1:]))
                        n_sys = int(np.sum(all_preds[:,1:]))
                        try:
                            precision = n_r / n_sys
                            recall = n_r / n_std
                            f1 = 2 * precision * recall / (precision + recall)
                        except ZeroDivisionError:
                            f1 = 0.0

                        if f1 > max_f1:
                            max_f1 = f1
                            print('f1: %f' % f1)
                            print('saving model')
                            path = saver.save(sess, os.path.join(self.model_path, 'ipre_bag_%d' % (self.bag)), global_step=0)
                            tempstr = 'have saved model to ' + path
                            print(tempstr)

        else:
            path = os.path.join(self.model_path, 'ipre_bag_%d' % self.bag) + '-0'
            tempstr = 'load model: ' + path
            print(tempstr)
            try:
                saver.restore(sess, path)
            except:
                raise ValueError('Unvalid model name')

            sent_test = self.load_sent('sent_test.txt')
            if self.bag:
                test_batchers = self.data_batcher(sent_test, 'bag_relation_test.txt', padding=True, shuffle=False)
            else:
                test_batchers = self.data_batcher(sent_test, 'sent_relation_test.txt', padding=True, shuffle=False)

            self.run_test(sess, test_batchers)


def main(_):
    tf.compat.v1.reset_default_graph()
    print('build model')
    '''
    #GPU版本
    gpu_options = tf.compat.v1.GPUOptions(visible_device_list=FLAGS.cuda, allow_growth=True)
    with tf.Graph().as_default():
        set_seed()
        sess = tf.compat.v1.Session(
            config=tf.compat.v1.ConfigProto(gpu_options=gpu_options, allow_soft_placement=True, intra_op_parallelism_threads=1, inter_op_parallelism_threads=1))
        with sess.as_default():
            initializer = tf.contrib.layers.xavier_initializer()
            with tf.variable_scope('', initializer=initializer):
                model = Baseline(FLAGS)
            sess.run(tf.global_variables_initializer())
            saver = tf.compat.v1.train.Saver(max_to_keep=None)
            model.run_model(sess, saver)
    '''
    with tf.Graph().as_default():
         set_seed()
         sess = tf.compat.v1.Session()
         with sess.as_default():
            initializer = tf.contrib.layers.xavier_initializer()
            with tf.compat.v1.variable_scope('', initializer=initializer):
                model = Baseline(FLAGS)
            sess.run(tf.global_variables_initializer())
            saver = tf.compat.v1.train.Saver(max_to_keep=None)
            model.run_model(sess, saver)

if __name__ == '__main__':
    tf.compat.v1.app.run()
