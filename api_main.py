from fastapi import FastAPI
import pandas as pd
from typing import Optional
from tempfile import NamedTemporaryFile as Temp
import os
from config import *
import nemo
import requests
import json
import networkx as nx
import bclm
from ne_evaluate_mentions import fix_multi_biose

os.environ['CUDA_VISIBLE_DEVICES'] = ''

## NCRF stuff
from utils.data import Data
import torch
from model.seqlabel import SeqLabel
from ncrf_main import evaluate


def get_ncrf_data_object(model_name): #, input_path, output_path):
    data = Data()
    model = MODEL_PATHS[model_name]
    data.dset_dir = model['dset']
    data.load(data.dset_dir)
    data.HP_gpu = False
    #data.raw_dir = input_path
    #data.decode_dir = output_path
    data.load_model_dir = model['model']
    data.nbest = 1
    return data


def load_ncrf_model(data):
    model = SeqLabel(data)
    print('loading model:', data.load_model_dir)
    model.load_state_dict(torch.load(data.load_model_dir, map_location=torch.device('cpu')))
    return model


def ncrf_decode(model, data, temp_input):
    data.raw_dir = temp_input
    #data.decode_dir = temp_output
    data.generate_instance('raw')
    _, _, _, _, _, preds, _ = evaluate(data, model, 'raw', data.nbest)
    if data.nbest==1:
        preds = [sent[0] for sent in preds]
    return preds
    
    
def get_sents(text, tokenized):
    if not tokenized:
        sents = nemo.tokenize_text(text)
    else:
        sents = [sent.split(' ') for sent in text.split('\n')]
    return sents
    
    
def create_input_file(text, path, tokenized):
    sents = get_sents(text, tokenized)
    nemo.write_tokens_file(sents, path, dummy_o=True)
    return sents


## YAP stuff
def yap_request(route, data, yap_url=YAP_API_URL, headers=YAP_API_HEADERS):
    return requests.get(yap_url+route, data=data, headers=headers).json()


def run_yap_hebma(tokenized_sentences):
    text = "  ".join([" ".join(sent) for sent in tokenized_sentences])
    data = json.dumps({"text": f"{text}  "})
    resp = yap_request('/yap/heb/ma', data)
    return resp['ma_lattice']
    
    
def run_yap_md(ma_lattice):
    data = json.dumps({'amblattice': ma_lattice})
    resp = yap_request('/yap/heb/md', data)
    return resp['md_lattice']
    
    
def run_yap_joint(tokenized_sentences):
    text = "  ".join([" ".join(sent) for sent in tokenized_sentences])
    data = json.dumps({"text": f"{text}  "})
    resp = yap_request('/yap/heb/joint', data)
    return resp
    
    
def get_biose_count(ner_multi_preds):
    bc = []
    for i, sent in enumerate(ner_multi_preds):
        for j, bio in enumerate(sent):
            bc.append([i+1, j+1, bio, len(bio.split('^'))])

    bc = pd.DataFrame(bc, columns=['sent_id', 'token_id', 
                                   'biose', 'biose_count'])
    return bc


def prune_lattice(ma_lattice, ner_multi_preds):
    bc = get_biose_count(ner_multi_preds)
    lat = nemo.read_lattices(ma_lattice)
    valid_edges = nemo.get_valid_edges(lat, bc, non_o_only=False, keep_all_if_no_valid=True)
    cols = ['sent_id', 'token_id', 'ID1', 'ID2']
    pruned_lat = lat[lat[cols].apply(lambda x: tuple(x), axis=1).isin(valid_edges)]
    pruned_lat = to_lattices_str(pruned_lat)
    return pruned_lat


def to_lattices_str(df, cols = ['ID1', 'ID2', 'form', 'lemma', 'upostag', 'xpostag', 'feats', 'token_id']):
    lat = ''
    for _, sent in df.groupby('sent_id'):
        for _, row in sent[cols].iterrows():
            lat += '\t'.join(row.astype(str).tolist())+'\n'
        lat += '\n'
    return lat
            
    
def soft_merge_bio_labels(ner_multi_preds, md_lattices):
    multitok_sents = bclm.get_sentences_list(get_biose_count(ner_multi_preds), ['biose'])
    md_sents = bclm.get_sentences_list(bclm.get_token_df(nemo.read_lattices(md_lattices), fields=['form'], token_fields=['sent_id', 'token_id'], add_set=False), ['token_id', 'form'])
    new_sents = []
    for (i, mul_sent), (sent_id, md_sent) in zip(multitok_sents.iteritems(), md_sents.iteritems()):
        new_sent = []
        for (bio,), (token_id, forms) in zip(mul_sent, md_sent):
            forms = forms.split('^')
            bio = bio.split('^')
            if len(forms) == len(bio):
                new_forms = (1, list(zip(forms,bio)))
            elif len(forms)>len(bio):
                dif = len(forms) - len(bio)
                new_forms = (2, list(zip(forms[:dif],['O']*dif)) + list(zip(forms[::-1], bio[::-1]))[::-1])
            else:
                new_forms = (3, list(zip(forms[::-1], bio[::-1]))[::-1])
            new_sent.extend(new_forms[1])
        new_sents.append(new_sent)
    return new_sents


def align_multi_md(ner_multi_preds, md_lattice):
    aligned_sents = soft_merge_bio_labels(ner_multi_preds, md_lattice) 
    return aligned_sents


# load all models
available_models = ['token-single', 'token-multi', 'morph']
loaded_models = {}
for model in available_models:
    m = {}
    m['data'] = get_ncrf_data_object(model)
    m['model'] = load_ncrf_model(m['data'])
    loaded_models[model] = m

available_commands = ['run_ner_model', 'multi_align_hybrid']


app = FastAPI()


@app.get("/")
def home():
    return {"error": "Please specify command"}


@app.get("/run_ner_model/")
def run_ner_model(sentences: str, model_name: str, tokenized: Optional[bool] = False):
    if model_name in available_models:
        model = loaded_models[model_name]
        with Temp() as temp_input:
            tok_sents = create_input_file(sentences, temp_input.name, tokenized)
            preds = ncrf_decode(model['model'], model['data'], temp_input.name)
        return { 
            'tokenized_text': tok_sents,
            'nemo_predictions': preds 
        }
    else:
        return {'error': f'model name "{model_name}" unavailable'}


@app.get("/multi_align_hybrid/")
def multi_align_hybrid(sentences: str, model_name: Optional[str] = 'token-multi', tokenized: Optional[bool] = False):
    if not 'multi' in model_name:
        return {'error': 'model must be "*multi*" for "multi_align_hybrid"'}
    model_out = run_ner_model(sentences, model_name, tokenized)
    tok_sents, ner_multi_preds = model_out['tokenized_text'], model_out['nemo_predictions']
    ma_lattice = run_yap_hebma(tok_sents)
    pruned_lattice = prune_lattice(ma_lattice, ner_multi_preds)
    md_lattice = run_yap_md(pruned_lattice) #TODO: this should be joint, but there is currently no joint on MA in yap api
    morph_aligned_preds = align_multi_md(ner_multi_preds, md_lattice)
    return { 
            'tokenized_text': tok_sents,
            'nemo_multi_predictions': ner_multi_preds,
            'ma_lattice': ma_lattice,
            'pruned_lattice': pruned_lattice,
            'md_lattice': md_lattice,
            'morph_aligned_predictions': morph_aligned_preds,
        } 
    
    
@app.get("/multi_to_single/")
def multi_to_single(sentences: str, model_name: Optional[str] = 'token-multi', tokenized: Optional[bool] = False):
    if not 'multi' in model_name:
        return {'error': 'model must be "*multi*" for "multi_to_single"'}
    model_out = run_ner_model(sentences, model_name, tokenized)
    tok_sents, ner_multi_preds = model_out['tokenized_text'], model_out['nemo_predictions']
    ner_single_preds = [[fix_multi_biose(label) for label in sent] for sent in ner_multi_preds]
    return { 
            'tokenized_text': tok_sents,
            'nemo_multi_predictions': ner_multi_preds,
            'single_predictions': ner_single_preds,
        } 


@app.get("/morph_yap/")
def morph_yap(sentences: str, model_name: Optional[str] = 'morph', tokenized: Optional[bool] = False):
    if not 'morph' in model_name:
        return {'error': 'model must be "*morph*" for "morph_yap"'}
    tok_sents = get_sents(sentences, tokenized)
    yap_out = run_yap_joint(tok_sents)
    md_sents = (bclm.get_sentences_list(nemo.read_lattices(yap_out['md_lattice']), ['form']).apply(lambda x: [t[0] for t in x] )).to_list()
    model = loaded_models[model_name]
    with Temp() as temp_input:
        nemo.write_tokens_file(md_sents, temp_input.name, dummy_o=True)
        morph_preds = ncrf_decode(model['model'], model['data'], temp_input.name)
    return { 
            'tokenized_text': tok_sents,
            'ma_lattice': yap_out['ma_lattice'],
            'md_lattice': yap_out['md_lattice'],
            'dep_tree': yap_out['dep_tree'],
            'morph_segmented_text': md_sents,
            'nemo_morph_predictions': morph_preds,
        } 


# @app.get("/run_separate_nemo/")
# def run_separate_nemo(command: str, model_name: str, sentence: str):
#     if command in available_commands:
#         if command == 'run_ner_model':
#             with Temp('r', encoding='utf8') as temp_output:
#                 nemo.run_ner_model(model_name, None, temp_output.name, text_input=sentence)
#                 output_text = temp_output.read()
#             return { 'nemo_output': output_text }
#     else: 
#         return {'error': 'command not supported'}
