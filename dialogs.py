# -*- coding: utf-8 -*-

import json
import re
import settings
import logging
import random
from redis import StrictRedis
from pymongo import MongoClient, DESCENDING, ASCENDING
from flask import url_for
from wechat.bot import UnexpectAnswer
from datetime import datetime, timedelta
from collections import defaultdict
from bson import json_util

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

ROUTER = {
    'text': [
        ('^[\?\？]$', 'show_help'),
        ('^[\?\？]{2}$', 'show_help_link'),
        ('^[!！]\d*$', 'show_updates'),
        ('^[!！]{2}\d*$', 'show_all_updates'),
        ('^[.。]$', 'show_follows'),
        # ('^\d{6}$', 'pin_login'),
        ('.*', 'search_keyword'),
    ],
    'event': [
        ('^subscribe$', 'active_user'),
        ('^unsubscribe$', 'deactive_user'),
        ('.*', 'show_help'),
    ],
}

TYPE_TXT = {
    'anime': '动画',
    'drama': '电视剧',
    'variety': '综艺',
}


def _keyword_summary(keyword):
    mongo_feeds = mongoCollection('feeds')
    mongo_series = mongoCollection('series')
    text = '[%s] %s' % (TYPE_TXT[keyword['type']], keyword['keyword'])
    text += '\n-----\n'
    feeds = list(mongo_feeds.find({
        'keyword_id': keyword['_id'], 
        'break_rules': {
            '$exists': False,
        },
    }).sort('upload_time', DESCENDING))
    text += '最后更新：%s \n' % feeds[0]['upload_time'].strftime('%Y-%m-%d')
    
    # 最近更新的ep，date_ep
    text += '更新至：' 
    last_ep_series = list(mongo_series.find({'keyword_id': keyword['_id']}, sort=[('season', DESCENDING), ('episode', DESCENDING), ('date_episode', DESCENDING)]))
    last_date_ep_series = list(mongo_series.find({'keyword_id': keyword['_id']}, sort=[('season', DESCENDING), ('date_episode', DESCENDING), ('episode', DESCENDING)]))
    if last_ep_series[0]['first_upload_time'] >= last_date_ep_series[0]['first_upload_time']:
        last_series = last_ep_series[0]
    else:
        last_series = last_date_ep_series[0]

    if 'season' in last_series and last_series['season'] != '-1':
        text += '第%s季 ' % last_series['season']
    if 'episode' in last_series:
        text += '第%s话 ' % last_series['episode']
    if 'date_episode' in last_series:
        text += last_series['date_episode']
    last_series_index = 0
    return text, feeds, last_ep_series, last_series
    
def _make_page(text_list, page_size, prefix='', suffix='--- 回复N翻页 ---', end_suffix=''):
    idx = 0
    show_list = text_list[idx: idx+page_size]
    while True:
        text = prefix
        if text:
            text += '\n'
        text += '\n'.join(show_list)
        
        idx += page_size
        show_list = text_list[idx: idx+page_size]
        if len(show_list) > 0:
            if suffix:
                text += '\n' + suffix
            yield ('TextMsg', text), False
        # 最后一页了，显示end_suffix
        else:
            if end_suffix:
                text += '\n' + end_suffix
            yield ('TextMsg', text), True
            return

# 从MONGO里面读取HELP_LINKS和HELP_MESSAGE
meta = mongoCollection('meta')
HELP_LINKS = ('NewsMsg', list(meta.find({'type': 'HELP_LINKS'}, sort=[('order', ASCENDING)])))
HELP_MESSAGE = '\n'.join( meta.find_one({'type': 'HELP_MESSAGE'})['content'] )

def show_help(to_user):
    yield None
    msg_content, is_replay = yield None
    return ('TextMsg', HELP_MESSAGE)

def show_help_link(to_user):
    yield None
    msg_content, is_replay = yield None
    return HELP_LINKS      

def show_all_updates(to_user):
    yield None
    msg_content, is_replay = yield None
    now_time = datetime.now()
    back_days = 7
    try:
        back_days = int(msg_content.replace("!", "").replace("！", ""))
    except ValueError:
        pass
    last_check_time = now_time - timedelta(days=back_days)
    mongo_feeds = mongoCollection('feeds')
    # 获取所有新资源
    updated_keywords = mongo_feeds.aggregate([
        {'$match': {
            'scrapy_time': { '$gte': last_check_time }, # 获取在上次爬取后新增的资源
            'upload_time': { '$gte': last_check_time - timedelta(days=5) }, # 同时upload_time必须在一周内,避免因搜索排名导致的误更新
            'break_rules': { '$exists': False },
            'analyzed': True,
        }},
        {'$group': {
            "_id": {
                "keyword": "$keyword_title",
            },
            "last_update": { "$max": "$upload_time" },
        }},
        {'$sort': {'last_update': -1}},
    ])
    summary_text_list = []
    for item in updated_keywords:
        keyword = item['_id']['keyword']
        last_update = item['last_update'].strftime('%Y-%m-%d')
        summary_text_list.append('%s => 更新时间：%s' % (keyword, last_update))

    if not summary_text_list:
        return ('TextMsg', '该时段无资源更新！' % last_check_time.strftime('%Y/%m/%d %H:%M'))
    if back_days > 0:
        prefix = '--- %s天内的所有更新 ---' % back_days
    
    for msg, is_last in _make_page(
        summary_text_list, 10, 
        prefix=prefix, 
        suffix= '--- 回复N翻页 ---',
    ):
        selected, is_replay = yield msg
        if selected in ['N', 'n'] and not is_last:
            continue
        else:
            raise UnexpectAnswer    

def show_updates(to_user):
    yield None
    msg_content, is_replay = yield None

    now_time = datetime.now()
    back_days = 0
    try:
        back_days = int(msg_content.strip('!').strip('！'))
    except ValueError:
        pass
    
    mongo_feeds = mongoCollection('feeds')
    mongo_series = mongoCollection('series')
    mongo_users = mongoCollection('users')
    mongo_keywords = mongoCollection('keywords')
    
    # 获取用户关注的keyword和它的最新剧集
    user = mongo_users.find_one({'open_id': to_user})
    if 'last_check_time' not in user:
        user['last_check_time'] = now_time
    # 没关注的用户显示提示
    if 'follow_keywords' not in user or not user['follow_keywords']:
        return ('TextMsg', '您还没有关注资源哦，回复剧名搜索或者回复".."列出所有资源~')
    if back_days == 0:
        last_check_time = user['last_check_time'] 
        if now_time - last_check_time > timedelta(days=30): # 最多显示1个月内的更新，避免内容过多
            last_check_time = now_time - timedelta(days=30)
    else:
        last_check_time = now_time - timedelta(days=back_days)
    logger.debug(last_check_time)
    
    lkey = settings.LAST_CHECK_KEY % to_user
    if not is_replay and back_days == 0:
        # 获取更新查看时间，并保存到REDIS缓存
        last_check_time = user['last_check_time'] 
        if now_time - last_check_time > timedelta(days=30): # 最多显示1个月内的更新，避免内容过多
            last_check_time = now_time - timedelta(days=30)
        logger.debug('Write update to mongo...')
        mongo_users.find_one_and_update(
            { 'open_id': to_user }, 
            {
                '$set': { 'last_check_time': now_time },
            }
        )
        last_check_time_str = datetime.strftime(last_check_time, '%Y-%m-%d %H:%M:%S')
        redis_db.setex(lkey, 300, last_check_time_str)
    elif back_days > 0:
        last_check_time = now_time - timedelta(days=back_days)
    else:
        # REPLAY情况下读取REDIS缓存获得更新时间
        try:
            redis_db.expire(lkey, 300)
            last_check_time_str = redis_db.get(lkey).decode('utf-8')
            last_check_time = datetime.strptime(last_check_time_str, '%Y-%m-%d %H:%M:%S')
        except Exception:
            raise UnexpectAnswer
            
    logger.debug(last_check_time)
    
    # 获取所有新资源
    new_feeds = list(mongo_feeds.find({
        'keyword_id': { '$in': user['follow_keywords'] },
        'scrapy_time': { '$gte': last_check_time }, # 获取在上次爬取后新增的资源
        'upload_time': { '$gte': last_check_time - timedelta(days=5) }, # 同时upload_time必须在一周内,避免因搜索排名导致的误更新
        'break_rules': {'$exists': False},
        'analyzed': True,
    }).sort('upload_time', DESCENDING))
    
    text_list = ['%(title)s\n%(href)s' % f for f in new_feeds]
    fid_list = [f['_id'] for f in new_feeds]
    
    summary_text_list = []
    for kid in user['follow_keywords']:
        keyword = mongo_keywords.find_one({'_id': kid})
        last_ep_series = list(mongo_series.find({'keyword_id': keyword['_id']}, sort=[('season', DESCENDING), ('episode', DESCENDING), ('date_episode', DESCENDING)]))
        last_date_ep_series = list(mongo_series.find({'keyword_id': keyword['_id']}, sort=[('season', DESCENDING), ('date_episode', DESCENDING), ('episode', DESCENDING)]))
        if not last_ep_series:
            continue
        if last_ep_series[0]['first_upload_time'] >= last_date_ep_series[0]['first_upload_time']:
            last_series = last_ep_series[0]
        else:
            last_series = last_date_ep_series[0]
        # last_series = last_series[0]
        if 'episode' in last_series:
            last_ep_text = '第%s话' % last_series['episode']
        else:
            last_ep_text = last_series['date_episode']
        new_feed_count = 0
        for fid in last_series['feeds']:
            if fid not in fid_list:
                break
        else:
            # 最新话资源都是这次在这次更新中=>更新了新一集
            summary_text_list.append('%s[%s] 更新至 %s' % (keyword['keyword'], TYPE_TXT[keyword['type']], last_ep_text))
            continue
        # 并不是新一集
        new_feed_count = 0
        for f in new_feeds:
            if f['keyword_id'] == kid:
                new_feed_count += 1
        if new_feed_count > 0:
            summary_text_list.append('%s[%s] 有%s个新资源' % (keyword['keyword'], TYPE_TXT[keyword['type']], new_feed_count))

    if not summary_text_list:
        return ('TextMsg', '从上次查看[%s]到现在，关注的资源没有更新哦' % last_check_time.strftime('%Y/%m/%d %H:%M'))
    if back_days > 0:
        prefix = '--- %s天内的更新 ---' % back_days
    else:
        prefix = '--- %s后的更新 ---' % last_check_time.strftime('%m/%d %H:%M')
    
    for msg, is_last in _make_page(
        summary_text_list, 10, 
        prefix=prefix, 
        suffix= '--- 回复N翻页，回复L显示详细列表 ---',
        end_suffix='--- 回复L显示详细列表 ---'
    ):
        selected, is_replay = yield msg
        if selected in ['N', 'n'] and not is_last:
            continue
        elif selected in ['L', 'l']:
            for list_msg, is_last in _make_page(
                text_list, 5, 
                suffix= '--- 回复N翻页 ---',
                end_suffix='--- 结束 ---'
            ):
                selected, is_replay = yield list_msg
                if selected in ['N', 'n'] and not is_last:
                    continue
                else:
                    raise UnexpectAnswer
        else:
            raise UnexpectAnswer        
       
def pin_login(to_user):
    yield None
    msg_content, is_replay = yield None
    
    mongo_users = mongoCollection('users')
    pin_key = settings.PIN_KEY % msg_content
    pin_val = redis_db.get(pin_key)
    if not pin_val:
        return ('TextMsg', '您输入的PIN码不存在')
    else:
        redis_db.setex(pin_key, 30, to_user)
        res = mongo_users.update({
            'open_id': to_user,
            'site': 'main',
        },{
            '$set': {
                'active': True,
                'open_id': to_user,
                'site': 'main',
            }
        }, upsert=True)
        user_exists = res['updatedExisting']
        if user_exists:
            return ('TextMsg', '登陆成功！浏览器将自动跳转。')
        else:
            return ('TextMsg', '欢迎使用点剧，浏览器将自动跳转。')

def show_follows(to_user):
    yield None # send none for start
    msg_content, is_replay = yield None # Initial value
    
    mongo_users = mongoCollection('users')
    mongo_keywords = mongoCollection('keywords')
    user = mongo_users.find_one({'open_id': to_user})
    if 'follow_keywords' not in user or not user['follow_keywords']:
        return ('TextMsg', '您还没有关注资源哦，回复剧名搜索或者回复"!!"随便看看吧~')
    follow_list = user['follow_keywords']
    keywords = list(mongo_keywords.find({'_id': {'$in': follow_list}}))
    num = 0
    text_list = []
    already_followed = {}
    for kw in keywords:
        text_list.append('%s. %s [%s]' % (num, kw['keyword'], TYPE_TXT[kw['type']]))
        num += 1
    for msg, is_last in _make_page(
        text_list, 10, 
        prefix='【回复F加数字(如F2)取关】\n--- 关注列表 ---',
        suffix='--- 回复N翻页 回复数字选择 ---', 
        end_suffix='--- 回复数字选择 ---'
    ):
        selected, is_replay = yield msg
        if selected in ['N', 'n'] and not is_last:
            continue
        try:
            while True:
                if selected[0] in ['F', 'f']:
                    selected = int(selected[1:])
                    keyword = keywords[selected]
                    logger.debug(keyword)
                    if not is_replay:
                        msg_type, msg_content = _try_follow(to_user, True, keyword)
                        clear_msg = re.sub('%s.*\n' % selected, '' ,msg[1])
                        msg_content = '%s\n(%s)' % (clear_msg, msg_content)
                    else:
                        msg_content = 'none'
                    selected, is_replay = yield ('TextMsg', msg_content)
                else:
                    logger.debug(selected)
                    break
            if selected in ['N', 'n'] and not is_last:
                continue
            selected = int(selected)
            if selected > len(keywords) or selected < 0:
                raise TypeError
            logger.debug(keywords[selected]['keyword'])
            raise UnexpectAnswer(keywords[selected]['keyword'])
        except UnexpectAnswer as e:
            raise e
        except Exception:
            raise UnexpectAnswer

def _try_follow(to_user, already_followed, keyword):
    mongo_users = mongoCollection('users')
    if already_followed:
        mongo_users.find_one_and_update({
            'open_id': to_user
        }, 
        {
            '$pull': {
                'follow_keywords': keyword['_id'],
            }
        })
        return ('TextMsg', '%s取关成功!' % keyword['keyword'])
    else:
        mongo_users.find_one_and_update({
            'open_id': to_user
        }, 
        {
            '$addToSet': { 'follow_keywords': keyword['_id'] },
        }, upsert=True)
        return ('TextMsg', '%s关注成功!' % keyword['keyword'])
            
def search_keyword(to_user):
    yield None # send none for start
    msg_content, is_replay = yield None # Initial value
    
    mongo_keywords = mongoCollection('keywords')
    mongo_feeds = mongoCollection('feeds')
    mongo_series = mongoCollection('series')
    mongo_users = mongoCollection('users')
    
    user = mongo_users.find_one({'open_id': to_user})
    
    shuffle = False
    msg_content = msg_content.strip()
    if msg_content in ['..', '。。', '.。', '。.']:
        msg_content = '.*'
        shuffle = True
    try:
        keywords = mongo_keywords.find({
            '$or': [
                {
                    'keyword': re.compile(msg_content.strip(), flags=re.I),
                },
                {
                    'alias': { 
                        '$elemMatch': { '$regex': msg_content.strip() , '$options': '$i' }
                    }
                }
            ],
            'valid_feed_count': {'$gt': 0},
            'status': 'activated',
        })
    except Exception:
        return ('TextMsg', '∑(っ °Д °;)っ请不要输入奇怪的东西啦，真的会死机的哦~~')
    keywords = [k for k in keywords]
    
    if shuffle and not is_replay:
        random.shuffle(keywords)
        rkey = settings.RECOMMEND_KEY % to_user
        redis_db.setex(rkey, 300, json_util.dumps(keywords))
    elif shuffle and is_replay :
        rkey = settings.RECOMMEND_KEY % to_user
        keywords = json_util.loads(redis_db.get(rkey).decode('utf-8'))
    
    count = len(keywords)
    already_followed = {}
    if count == 0:
        return ('TextMsg', '没有找到"%s"相关的资源，如需添加新资源请点击: https://www.wenjuan.net/s/mmeYZj/' % msg_content.strip())
    elif count > 1:
        text_list = []
        num = 0
        for kw in keywords:
            if 'follow_keywords' in user and kw['_id'] in user['follow_keywords']:
                text_line = '%s. %s [%s] [已关注]' % (num, kw['keyword'], TYPE_TXT[kw['type']])
                already_followed[kw['_id']] = True
            else:
                text_line = '%s. %s [%s]' % (num, kw['keyword'], TYPE_TXT[kw['type']])
                already_followed[kw['_id']] = False
            text_list.append(text_line)
            num += 1
        for msg, is_last in _make_page(
            text_list, 10, 
            prefix='【回复F加数字(如F2)可以直接关注/取关资源哦】\n--- 找到了多个资源 ---', 
            suffix='--- 回复N翻页 回复数字选择 ---', 
            end_suffix='--- 回复数字选择 ---'
        ):
            selected, is_replay = yield msg
            
            if selected in ['N', 'n'] and not is_last:
                continue
            try:
                while True:
                    if selected[0] in ['F', 'f']:
                        selected = int(selected[1:])
                        keyword = keywords[selected]
                        if not is_replay:
                            msg_type, msg_content = _try_follow(to_user, already_followed[keyword['_id']], keyword)
                            if '关注' in msg_content:
                                clear_msg = re.sub('(%s.*?\]).*' % selected, lambda match: '%s [已关注]' % match.group(1) ,msg[1])
                            else:
                                clear_msg = re.sub('(%s.*?\]).*' % selected, lambda match: match.group(1) ,msg[1])
                            msg_content = '%s\n(%s)' % (clear_msg, msg_content)
                        else:
                            msg_content = 'none'
                        selected, is_replay = yield ('TextMsg', msg_content)
                    else:
                        logger.debug(selected)
                        break
                if selected in ['N', 'n'] and not is_last:
                    continue
                selected = int(selected)
                if selected > count or selected < 0:
                    raise TypeError
                break
            except Exception:
                raise UnexpectAnswer
    else:
        selected = 0
    keyword = keywords[selected]

    text, feeds, all_episodes, last_series = _keyword_summary(keyword)
    
    text += '\n-----\n'
    text += '回复L列出所有资源\n'
    # 查询用户是否关注了这个资源
    if 'follow_keywords' in user and keyword['_id'] in user['follow_keywords']:
        already_followed[keyword['_id']] = True
        text += '您已关注该资源，回复F取消关注'
    else:
        already_followed[keyword['_id']] = False
        text += '回复F关注该资源'
    selected, is_replay = yield ('TextMsg', text)
    
    feeds = mongo_feeds.find({
        'keyword_id': keyword['_id'],
        'break_rules': {'$exists': False}
    }).sort('upload_time', DESCENDING)
    text_list = ['%(title)s\n%(href)s' % feed for feed in feeds]
    if already_followed[keyword['_id']]:
        suffix_follow = '回复F取关'
    else:
        suffix_follow = '回复F关注'
    if selected in ['L', 'l']:
        for msg, is_last in _make_page(
            text_list, 5, 
            suffix='--- 回复N翻页 %s ---' % suffix_follow, 
            end_suffix='--- %s ---' % suffix_follow
        ):
            selected, is_replay = yield msg
            
            if selected in ['N', 'n'] and not is_last:
                continue
            elif selected in ['F', 'f']:
                return _try_follow(to_user, already_followed[keyword['_id']], keyword)
            else:
                raise UnexpectAnswer
    elif selected in ['F', 'f']:
        return _try_follow(to_user, already_followed[keyword['_id']], keyword)
    else:
        raise UnexpectAnswer
    
    return ('TextMsg', text)

def active_user(to_user):
    yield None # send none for start
    msg_content, is_replay = yield None # Initial value
    
    mongo_users = mongoCollection('users')
    res = mongo_users.update({
        'open_id': to_user,
        'site': 'main',
    },{
        '$set': {
            'active': True,
            'open_id': to_user,
            'site': 'main',
            'last_check_time': datetime.now() - timedelta(days=365),
        }
    }, upsert=True)
    # return ('TextMsg', HELP)
    return HELP_LINKS
   
def deactive_user(to_user):
    yield None # send none for start
    msg_content = yield None # Initial value
    mongo_users = mongoCollection('users')
    mongo_users.update({
        'open_id': to_user,
        'site': 'main',
    },{
        '$set': {
            'active': False,
            'site': 'main',
        }
    })
    return ('TextMsg', 'Bye')
