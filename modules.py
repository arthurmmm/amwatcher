#!/usr/bin/env python3
#coding=utf-8

class User(object):
    _id = ''
    display_name = ''
    open_id = ''
    active = True
    role = 'user'
    
    def __init__(self, user_info):
        if not user_info:
            return None
        for k,v in user_info.items():
            setattr(self, k, v)
    
    def is_authenticated(self):
        # res = (self.role == 'admin')
        return True
    def is_active(self):
        return self.active
    def is_anonymous(self):
        return False
    def get_id(self):
        return str(self._id).encode('utf-8')
    def __repr__(self):
        return '<User %s(%s)>' % (self.open_id, self.display_name)