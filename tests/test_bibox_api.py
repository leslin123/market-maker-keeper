import hmac
import hashlib
import json, requests

def getSign(data,secret):
    result = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.md5).hexdigest()
    return result

def doApiRequestWithApikey(url, cmds, api_key, api_secret):
    s_cmds = json.dumps(cmds)
    sign = getSign(s_cmds,api_secret)
    proxies = {"http": "127.0.0.1:1087", "https": "127.0.0.1:1087"}
    r = requests.post(url, data={'cmds': s_cmds, 'apikey': api_key,'sign':sign} ,proxies=proxies)
    print(r.text)

def post_order(api_key,api_secret,cmds):
    url = "https://api.bibox.com/v1/orderpending"
    doApiRequestWithApikey(url,cmds,api_key,api_secret)


api_key = 'bad03dcf8d6e26e064814729239a574244f0590b'
api_secret = 'ddf034b8fc4868f7eb6cdec830d33b7c495c0e2e'


pair = 'BIX_ETH'
account_type = 0 # common
order_type = 2 # xianjiadan
order_side = 2 # sell

pay_bix = 1 #是否bix抵扣手续费，0-不抵扣，1-抵扣
price = 0.00198810
amount = 10
money = 0.11688

cmds = [
    {
        'cmd':"orderpending/trade",
        'body':{
            'pair':pair,
            'account_type':account_type,
            'order_type':order_type,
            'order_side':order_side,
            'pay_bix':pay_bix,
            'price':price,
            'amount':amount,
            'money':money
        }
    }
]
print(cmds)
post_order(api_key,api_secret,cmds)