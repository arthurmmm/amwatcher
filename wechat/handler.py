# -*- coding: utf-8 -*-

import json
import re
import settings
import logging
from redis import StrictRedis
from pymongo import MongoClient
from flask import url_for

from . import reply, receive

logger = logging.getLogger('__main__')

local = settings.LOCAL_CONFIG
redis_db = StrictRedis(
    host=local['REDIS_HOST'], 
    port=local['REDIS_PORT'], 
    password=local['REDIS_PASSWORD'],
    db=local['REDIS_DB']
)
def mongoCollection(cname):
    mongo_client = MongoClient(local['MONGO_URI'])
    mongo_db = mongo_client[local['MONGO_DATABASE']]
    return mongo_db[cname]

def _redis_replay(key, dialog):
    ''' Replay dialog based on redis history
    '''
    hist = redis_db.get(key)
    if not hist:
        # hist = []
        raise Exception('Empty hist!')
    else:
        hist = json.loads(hist.decode('utf-8'))[1:]
    for step in hist:
        dialog.send(step)
    return dialog
        
def _redis_send(key, dialog, msg, expire=60):
    ''' Send msg to dialog and store history to redis
    '''
    hist = redis_db.get(key)
    if not hist:
        logger.debug(dialog.__name__)
        # 第一个元素用于存generator的名字，其余均为消息记录
        hist = [dialog.__name__]
    else:
        hist = json.loads(hist.decode('utf-8'))
    hist.append(msg)
    redis_db.setex(key, expire, json.dumps(hist))
    logger.debug(dialog)
    return dialog.send(msg)
    

ROOT_ROUTER = {
    'text': [
        ('^[\?\？]$', 'show_help'),
        ('^[!！]$', 'get_status'),
        # ('^\d{6}$', 'pin_login'),
        # ('^菜单$', show_menu),
        ('.*', 'show_help'),
    ],
    'event': [
        # ('^subscribe$', active_user),
        # ('^unsubscribe$', deactive_user),
        ('.*', 'show_help'),
    ],
}

def _new_dialog(msg_type, msg_content, to_user):
    hkey = settings.CONTEXT_KEY % to_user
    redis_db.delete(settings.CONTEXT_KEY % to_user)
    # 根据router重新选择并构造回复器
    router = ROOT_ROUTER[msg_type]
    for pattern, fname in router:
        regex = re.compile(pattern)
        if regex.match(msg_content):
            dialog = getattr(DialogFactory, fname)(to_user)
            break
    else:
        raise Exception('Router not found')
    # 初始化操作
    dialog.send(None)
    _redis_send(hkey, dialog, msg_content)
    return dialog
    
def _replay_dialog(hist, to_user):
    hkey = settings.CONTEXT_KEY % to_user
    # 从hist中获取这个消息的处理器
    dialog_handler_name = json.loads(hist.decode('utf-8'))[0]
    dialog = getattr(DialogFactory, dialog_handler_name)(to_user)
    # 重现上下文
    dialog.send(None)
    _redis_replay(hkey, dialog)
    return dialog

def answer(msg):
    # Extract msg
    msg_type = msg.MsgType
    to_user = msg.FromUserName
    from_user = msg.ToUserName
    if isinstance(msg, receive.TextMsg):
        msg_content = msg.Content.decode('utf-8')
    elif isinstance(msg, receive.EventMsg):
        msg_content = msg.Event.decode('utf-8')
    else:
        msg_content = 'default'
        
    hkey = settings.CONTEXT_KEY % to_user
    # redis_db.delete(hkey) # TODO - TEST
    hist = redis_db.get(hkey)
    # 新会话或者会话超时，创建新会话
    if not hist:
        dialog = _new_dialog(msg_type, msg_content, to_user)
        logger.debug('new_dialog')
    # 存在会话记录，重现上下文
    else:
        logger.debug('replay_dialog')
        try:
            dialog = _replay_dialog(hist, to_user)
        except StopIteration:
            logger.error('会话记录错误..重新创建会话..')
            dialog = _new_dialog(msg_type, msg_content, to_user)
    # 发送消息
    try:
        type, msg = _redis_send(hkey, dialog, msg_content)
    except StopIteration as e:
        # 会话已结束，删去redis中的记录
        type, msg = e.value
        redis_db.delete(settings.CONTEXT_KEY % to_user)
    
    wechat_reply = getattr(reply, type)
    return wechat_reply(
        to_user, 
        from_user, 
        msg
    )
    
class DialogFactory(object):
    @staticmethod
    def show_help(to_user):
        yield None # send none for start
        msg_content = yield None # Initial value
        msg_content = yield ('TextMsg', '# TODO - 显示帮助文档')
        return ('TextMsg', msg_content)
            
    @staticmethod
    def get_status(to_user):
        yield None # send none for start
        msg_content = yield None # Initial value
        return ('TextMsg', '# TODO - 显示更新')