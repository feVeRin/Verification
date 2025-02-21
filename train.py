import os
import importlib
import gc
import torch

from validation import *
import data.kdataset as kdataset
from SpeakerNet import SpeakerNet
from util import *

# ===================
# Training Wrapper
# ===================
def train(
    config, 
    max_epoch, 
    batch_size, 
    num_worker, 
    base_lr, 
    base_path, 
    device,
    ckpt = False,
    ckpt_name = None
    ):
    """
    Model training wrapper. 
    Iterate train_step() with given max_epoch size 
    
    Args:
        config : configuration (.yaml)
        max_epoch : max epoch size
        batch_size : model batch size
        num_worker : numbers of dataset loader worker
        base_lr : learning rate
        base_path : df file path (train_df, enr_df)
        device : device (CPU/GPU)
        ckpt : Continue training from the chekpoint (Default = False)
        ckpt_name : Checkpoint (.pt) file name (Default = None)
        
    Returns:
        None
    """
    # =====================================
    # Dataset Setting
    # =====================================
    print('Setting Train Dataset...')
    asv_dataset = kdataset.asv_dataset(*config['TRAIN_DATASET'].values())
    
    train_loader = torch.utils.data.DataLoader(
        asv_dataset,
        batch_size = batch_size,
        num_workers = num_worker,
        pin_memory=True,
        drop_last=True,
        shuffle=True
    )
    
    # =====================================
    # Model Setting
    # =====================================
    print()
    print('Setting Model...')
    
    # feature extractor
    feature_extractor = importlib.import_module('preprocessing.mel_transform').__getattribute__("feature_extractor")
    feature_extractor = feature_extractor(*config['FEATURE_EXTRACTOR'].values()).to(device)
    
    # spectral augmentation
    spec_aug = importlib.import_module('preprocessing.spec_aug').__getattribute__("spec_aug")
    spec_aug = spec_aug(*config['SPEC_AUG'].values()).to(device)
    
    # TDNN
    model_cfg = config['MODEL']
    model = importlib.import_module('models.NeXt_TDNN').__getattribute__("MainModel")
    model =  model(
        depths = model_cfg['depths'], 
        dims = model_cfg['dims'],
        kernel_size = model_cfg['kernel_size'],
        block = model_cfg['block']).to(device)
    
    # aggregation
    aggregation = importlib.import_module('aggregation.vap_bn_tanh_fc_bn').__getattribute__("Aggregation")
    aggregation = aggregation(*config['AGGREGATION'].values()).to(device)
    
    # loss function
    loss_function = importlib.import_module("loss.aamsoftmax").__getattribute__("LossFunction")
    loss_function = loss_function(*config['LOSS'].values())
    
    # model wrapper (SpeakerNet)
    speaker_net = SpeakerNet(feature_extractor = feature_extractor,
                             spec_aug = spec_aug, 
                             model = model,
                             aggregation = aggregation,
                             loss_function = loss_function).to(device)
    
    # optimizer
    optimizer = importlib.import_module("optimizer." + 'adamw').__getattribute__("Optimizer")
    optimizer = optimizer(speaker_net.parameters(), lr = base_lr*batch_size, weight_decay = 0.01,)
    
    # scheduler
    scheduler = importlib.import_module("scheduler." + 'steplr').__getattribute__("Scheduler")
    scheduler = scheduler(optimizer, step_size = 10, gamma = 0.8)
    
    # Model Summary
    print('===============================')
    print()
    print('Model Summary...')
    get_model_param_mmac(speaker_net, int(160*300 + 240), device)
    
    # =====================================
    # Model Training
    # =====================================
    print('===============================')
    print()
    print('Model Training...')
    
    if ckpt:
        # if checkpoint available
        print('Load Previous Checkpoint..')
        checkpoint = torch.load(os.path.join(config['CHECKPOINT']['ckpt_path'], ckpt_name))
        
        speaker_net.load_state_dict(checkpoint["model"], strict=False)
        optimizer.load_state_dict(checkpoint["optimizer"])
        scheduler.load_state_dict(checkpoint["scheduler"])
        ckpt_epoch = checkpoint["epoch"]
        
        # starts with chekpoint
        for epoch in range(ckpt_epoch+1, max_epoch):
            train_step(config, 
                       epoch, 
                       train_loader, 
                       speaker_net, 
                       optimizer, 
                       loss_function, 
                       scheduler, 
                       base_path, 
                       device)
    else:
        # scratch 
        for epoch in range(max_epoch):
            train_step(config, 
                       epoch, 
                       train_loader, 
                       speaker_net, 
                       optimizer, 
                       loss_function, 
                       scheduler, 
                       base_path, 
                       device)
    

# ===================
# Train Each Step
# ===================
def train_step(
    config, 
    epoch, 
    loader, 
    model, 
    optimizer, 
    loss_function, 
    scheduler, 
    base_path, 
    device
    ):
    """
    Training step
    
    Args:
        config : configuration (.yaml)
        epoch : current epoch
        loader : train loader
        model : verification model (speaker_net)
        optimizer : optimizer
        loss_function : loss function
        scheduler : training scheduler
        base_path : df file path (train_df, enr_df)
        device : device (CPU/GPU)
        
    Returns:
        None
    """
    
    losses = 0
    model.train()
    
    gc.collect()
    torch.cuda.empty_cache()
    
    # =====================================
    # Model Training
    # =====================================
    print('=== Epoch : {0} ==='.format(epoch))
    pbar = tqdm.tqdm(loader)
    for idx, (x, y) in enumerate(pbar):
        optimizer.zero_grad()
        
        spk_emb = model(x.to(device))
        loss, _ = loss_function(spk_emb, y.to(device))
        losses += loss.item()
        
        loss.backward()
        optimizer.step()
        
        if idx % 1000 == 0:
            pbar.set_postfix_str('{0} step loss : {1}'.format(idx, loss))
    
    scheduler.step()
    print('-- Epoch {0} loss : {1}'.format(epoch, losses/len(loader)))
    
    # =====================================
    # Validation
    # =====================================
    cos_eer, euc_eer, cos_dcf, euc_dcf = validation(model, base_path, device)
    print('Cosine EER : {0}, Euclidean EER : {1}'.format(cos_eer, euc_eer))
    print('Cosine MinDCF : {0}, Euclidean MinDCF : {1}'.format(cos_dcf, euc_dcf))
    
    # =====================================
    # Chekpoint Saving
    # =====================================
    ckpt_name = config['CHECKPOINT']['filename'].format(epoch)
    torch.save({'epoch' : epoch,
                'model' : model.state_dict(),
                'optimizer' : optimizer.state_dict(),
                'scheduler' : scheduler.state_dict(),
                'loss' : losses/len(loader),
                'cos_eer' : cos_eer,
                'euc_eer' : euc_eer,
                'cos_dcf' : cos_dcf,
                'euc_dcf' : euc_dcf,
                }, os.path.join(config['CHECKPOINT']['ckpt_path'], ckpt_name))
    print('-- Epoch {0} ckpt saved..'.format(epoch))
    print()
