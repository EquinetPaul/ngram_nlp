import sys
import os
import logging
import uuid
import time
from joblib import Parallel
from joblib import delayed as dlyed
import multiprocessing
from dask.distributed import Client
from dask import delayed
from dask.distributed import progress

import vocabulary
import ngram

import pandas as pd
import json
from glob import glob
from tqdm import tqdm

logging.basicConfig(format='%(levelname)s %(asctime)s - %(message)s', level=logging.INFO)

def load_config():
    """
    Fonction permettant de charger le fichier de configuration config.json et de le charger en tant que dictionnaire Python.
    """
    try:
        logging.info("Loading configuration...")
        with open('config.json') as json_config_file:
            config = json.load(json_config_file)
        logging.info("Loaded.")
        return config
    except Exception as e:
        logging.error("Error while loading configuration file 'config.json'.")
        logging.error(e)
        logging.error("Exiting.")
        sys.exit(1)
        raise

def load_data(parameters):
    """
    Fonction permettant de charger les données en fonction des paramètres donnés dans le fichier de configuration.

    :param path: Chemin vers le dossier ou le fichier à traiter
    :param data_type: Type de donnée
    :param params: Dictionnaire des paramètres d'ouverture/selection

    Type de données pris en compte: DataFrame, Fichiers
    """
    try:
        path = parameters["source"]
        logging.info("Loading Data...")
        data_type = parameters["data_type"]
        if data_type == "df":
            df = pd.read_csv(path, **parameters["source_params"]["df_parameters"])
            data = df[parameters["source_params"]["df_column"]].to_list()

        if data_type == "files":
            files_paths = glob(path+"*"+parameters["file_extension"])
            data = [open(file, encoding="utf-8", mode="r").read() for file in tqdm(files_paths)]

        logging.info("Loaded.")
        return data
    except Exception as e:
        logging.error(f"Error while loading data in {path}.")
        logging.error(e)
        logging.error("Exiting.")
        sys.exit(1)
        raise
    pass

def load_or_create_vocabulary(data, name):
    """
    Fonction permettant de charger ou de créer le vocabulaire associé à un jeu de donnée.

    :param data: Données sur lesquelles créer le vocabulaire
    :param name: Nom du projet, pour récupérer le vocabulaire si déjà crée

    :return: le vocabulaire venant d'être chargé ou crée.
    """
    vocab_file_name = name + ".vocab"
    path_vocab = os.path.join("data", "vocabs", vocab_file_name)

    vocab_exists = os.path.exists(path_vocab)

    vocab = vocabulary.Vocabulary()

    try:
        if vocab_exists:
            logging.info(f"Vocabulary loaded from {path_vocab}")
            vocab.load(path_vocab)
        else:
            logging.info("Creating vocabulary...")
            for text in tqdm(data):
                vocab.update(str(text))
            logging.info(f"Vocabulary created and saved in {path_vocab}")
            vocab.save(path_vocab)
        return vocab
    except Exception as e:
        logging.error(f"Error while loading or saving vocabulary on {path_vocab}.")
        logging.error(e)
        logging.error("Exiting.")
        sys.exit(1)
        raise

def train_ngram(n, text, vocab):
    gram = ngram.Ngram(n)
    text = vocab.chain_to_ids(text)
    gram.train(text)

    unique_id = uuid.uuid4()
    gram.save("data/temp/"+str(unique_id)+".tmp")

def merge_grams(gram1, gram2):
    for key, value in gram2.chain_frequency.items():
        if key not in gram1.chain_frequency:
            gram1.chain_frequency[key] = value
        else:
            for sub_key, sub_value in value.items():
                if sub_key not in gram1.chain_frequency[key]:
                    gram1.chain_frequency[key][sub_key] = sub_value
                else:
                    gram1.chain_frequency[key][sub_key] += sub_value
    return gram1

def merge_models_incrementally(models_to_merge):
    while len(models_to_merge) > 1:
        new_models = []

        # Fusionner les modèles par paires
        for i in range(0, len(models_to_merge), 2):
            if i + 1 < len(models_to_merge):
                model1 = models_to_merge[i]
                model2 = models_to_merge[i + 1]
                merged_model = merge_grams(model1, model2)
                new_models.append(merged_model)
            else:
                # S'il reste un modèle impair, ajoutez-le simplement à la liste des nouveaux modèles
                new_models.append(models_to_merge[i])

        models_to_merge = new_models

    return models_to_merge[0]

def preprocessing(text):
    text = text.strip()
    text = text.replace("\n", " ")
    " ".join([t for t in text.split()])
    return text


def main():
    # Create Folders
    os.makedirs("data/ngram", exist_ok=True)
    os.makedirs("data/vocabs", exist_ok=True)
    os.makedirs("data/temp", exist_ok=True)

    config = load_config()
    name = config["generate_name"]
    data = load_data(config)
    logging.info(f"{len(data)} documents loaded.")

    logging.info("Preprocessing...")
    data = [preprocessing(text) for text in tqdm(data)]

    vocab = load_or_create_vocabulary(data, config["generate_name"])

    ngram_min = config["ngram_range_min"]
    ngram_max = config["ngram_range_max"]

    logging.info(f"Training Ngram range({ngram_min},{ngram_max})")
    total_start_time = time.time()

    if config["dask_distributed"]:
        logging.info("Starting Dask session...")
        local_scheduler_address = config["local_scheduler_address"]
        client = Client(local_scheduler_address)
        workers = client.scheduler_info()['workers']
        nthreads_total = sum(worker['nthreads'] for worker in workers.values())
        logging.info(f"Active Workers: {len(workers)}, Total Threads: {nthreads_total}")
        logging.info("Sending Vocabulary to workers...")
        distributed_vocab = client.scatter(vocab, broadcast=True)

    for n in range(ngram_min, ngram_max+1):
        [os.remove(path) for path in glob("data/temp/*.tmp")]
        start_time = time.time()

        # Training with Dask
        if not config["dask_distributed"]:
            logging.info(f"Starting Training Ngram, n={n} on local CPU")

            # Training parallelized
            if config["parallelize"]:
                logging.info(f"Parallel Processes: {multiprocessing.cpu_count()} ")
                Parallel(n_jobs=-1)(dlyed(train_ngram)(n, str(text), vocab) for text in data)

            else:
                # Training on single CPU
                [train_ngram(n, str(text), vocab) for text in tqdm(data)]

        else:
            # Commencer la session Dask
            scheduler_address = config["scheduler_address"]
            logging.info(f"Starting Training, n={n} on Dask Cluster.")
            logging.info(f"Scheduler IP: {scheduler_address}")

            client = Client(local_scheduler_address)
            workers = client.scheduler_info()['workers']
            nthreads_total = sum(worker['nthreads'] for worker in workers.values())

            logging.info(f"Active Workers: {len(workers)}, Total Threads: {nthreads_total}")

            results = [delayed(train_ngram)(text, distributed_vocab) for text in data]

            futures = client.compute(results)

            progress(futures)

        end_time = time.time()
        logging.info(f"Training finished in {round(end_time-start_time,2)}s")

        logging.info("Libération de la mémoire...")
        del vocab
        del data
        logging.info("Mémoire libérée.")

        # Prepare Mergine
        output_path = f"data/ngram/{name}/{n}"
        os.makedirs(output_path, exist_ok=True)
        [os.remove(path) for path in glob(f"{output_path}/*.ngram")]

        models_to_merge = glob("data/temp/*.tmp")
        logging.info(f"Merging {len(models_to_merge)} files...")
        start_time = time.time()

        gram_final = ngram.Ngram()
        for model_path in tqdm(models_to_merge):
            sub_gram = ngram.Ngram()
            sub_gram.load(model_path)
            gram_final = merge_grams(gram_final, sub_gram)
            os.remove(model_path)

        end_time = time.time()
        logging.info(f"Merging finished in {round(end_time-start_time,2)}s")

        logging.info(f"Saving model...")
        gram_final.save(output_path+f"/model_{n}.ngram")
        logging.info(f"Saved in {output_path}.")

        logging.info("Libération de la mémoire...")
        del gram_final
        logging.info("Mémoire libérée.")

        # Remove temp files
        [os.remove(path) for path in glob("data/temp/*.tmp")]

        total_end_time = time.time()
        logging.info(f"All Procedure finished in {round(total_end_time-total_start_time,2)}s")


if __name__ == '__main__':
    main()
