# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2017-2018 reverendus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import logging
import sys

from market_maker_keeper.limit import History
from market_maker_keeper.order_book import OrderBookManager
from market_maker_keeper.order_history_reporter import OrderHistoryReporter, create_order_history_reporter
from market_maker_keeper.price_feed import PriceFeedFactory
from market_maker_keeper.reloadable_config import ReloadableConfig
from market_maker_keeper.spread_feed import create_spread_feed
from market_maker_keeper.util import setup_logging
from pyexchange.bibox import BiboxApi, Order
from pymaker.lifecycle import Lifecycle
from pymaker.numeric import Wad
import random
import time
import json
from market_maker_keeper.util import Logger

from market_maker_keeper.band import NewOrder

class BiboxMarketSurfer:
    """Keeper acting as a market maker on Bibox."""

    logger = logging.getLogger()

    def __init__(self, args: list):
        parser = argparse.ArgumentParser(prog='bibox-market-maker-keeper')
        
        parser.add_argument("--config", type=str, required=True,
                            help="Bands configuration file")
        
        parser.add_argument("--pair", type=str, required=True,
                            help="Token pair (sell/buy) on which the surfer will operate")
        
        parser.add_argument("--output-path", type=str, required=True,
                            help="output file path of the completed order result")

        # reserved for old program
        # parser.add_argument("--price-feed", type=str, required=True,
        #                     help="Source of price feed")

        parser.add_argument("--price-feed-expiry", type=int, default=120,
                            help="Maximum age of the price feed (in seconds, default: 120)")

        parser.add_argument("--spread-feed", type=str,
                            help="Source of spread feed")

        parser.add_argument("--spread-feed-expiry", type=int, default=3600,
                            help="Maximum age of the spread feed (in seconds, default: 3600)")

        parser.add_argument("--order-history", type=str,
                            help="Endpoint to report active orders to")

        parser.add_argument("--order-history-every", type=int, default=30,
                            help="Frequency of reporting active orders (in seconds, default: 30)")

        parser.add_argument("--refresh-frequency", type=int, default=3,
                            help="Order book refresh frequency (in seconds, default: 3)")

        parser.add_argument("--debug", dest='debug', action='store_true',
                            help="Enable debug output")

        self.arguments = parser.parse_args(args)
        setup_logging(self.arguments)

        self.orderlog = Logger(self.arguments.output_path + "SurferResult_" + self.arguments.pair, level='info')

        try:
            f = open(self.arguments.config, 'r')
            config = json.loads(f.read())
            f.close()

            self.bibox_api = BiboxApi(api_server=config["bibox_api_server"],
                                      api_key=config["bibox_api_key"],
                                      secret=config["bibox_secret"],
                                      timeout=config["bibox_timeout"])
    
            for pair in config['pairs']:
                if pair['pair'] == self.arguments.pair:
                    self.total_amount = pair["total_amount"]
                    # percent of total amount of each transaction or each order
                    self.each_order_percent = pair["each_order_percent"]
                    self.arbitrage_percent = pair["arbitrage_percent"]
                    # the order count of sell or buy bands must less than limit
                    self.band_order_limit = pair["band_order_limit"]
            
        except Exception as e:
            logging.getLogger().warning(f"Config file is invalid ({e}). Treating the config file as it has no bands.")
        
        self.history = History()
        self.bands_config = ReloadableConfig(self.arguments.config)
        # self.price_feed = PriceFeedFactory().create_price_feed(self.arguments)
        self.spread_feed = create_spread_feed(self.arguments)
        self.order_history_reporter = create_order_history_reporter(self.arguments)
        
        self.local_orders = []
        self.each_order_amount=self.total_amount * self.each_order_percent

        # To implement abstract function with different exchanges API
        self.order_book_manager = OrderBookManager(refresh_frequency=self.arguments.refresh_frequency)
        self.order_book_manager.get_orders_with(lambda: self.bibox_api.get_orders(pair=self.pair(), retry=True))
        self.order_book_manager.get_balances_with(lambda: self.bibox_api.coin_list(retry=True))
        self.order_book_manager.cancel_orders_with(lambda order: self.bibox_api.cancel_order(order.order_id))
        self.order_book_manager.enable_history_reporting(self.order_history_reporter, self.our_buy_orders, self.our_sell_orders)
        self.order_book_manager.start()

    def main(self):
        # Place new orders while initialize the whole surfer system
        self.initialize_orders(self.each_order_amount, self.arbitrage_percent, self.band_order_limit)
        time.sleep(5) # wait for order book manager to get placed orders 足够时间保证系统稳定返回
        self.local_orders = self.order_book_manager.get_order_book().orders
        
        with Lifecycle() as lifecycle:
            lifecycle.initial_delay(10)
            lifecycle.every(10, self.synchronize_orders)
            lifecycle.on_shutdown(self.shutdown)

    def initialize_orders(self, each_order_amount, arbitrage_percent, band_order_limit):
        orders = []
        i = 1
        base_price = self.bibox_api.get_last_price(self.pair())
        while band_order_limit+1 > i:
            # place sell order
            price = float(base_price) * (1 + arbitrage_percent*i)
            # pay_amount = Wad.min(band.avg_amount - total_amount, our_sell_balance, limit_amount)
            pay_amount = each_order_amount * self.amount_disguise() #bix amount
            pay_amount = pay_amount + self.suffix_amount_identify()
            buy_amount = pay_amount * price #eth money
            print(pay_amount)
            
            orders.append(NewOrder(is_sell=True, price=Wad.from_number(price), pay_amount=Wad.from_number(pay_amount),
                                   buy_amount=Wad.from_number(buy_amount),
                                       confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))
            
            # place buy order, pay attention to rotate bix - eth
            price = float(base_price) * (1 - arbitrage_percent*i)
            # pay_amount = Wad.min(band.avg_amount - total_amount, our_sell_balance, limit_amount)
            tmp = each_order_amount * self.amount_disguise()
            pay_amount = tmp * price #eth money 25
            buy_amount = tmp  #bix amount 0.05
            buy_amount = buy_amount + self.suffix_amount_identify()
            print(buy_amount)
            orders.append(NewOrder(is_sell=False, price=Wad.from_number(price), pay_amount=Wad.from_number(pay_amount),
                                   buy_amount=Wad.from_number(buy_amount),
                                       confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))
            i = i + 1
        
        self.place_orders(orders)
        # 偶尔有 bug，提交的完成慢，导致 local orders 比 下一次 获取回来少， initial_delay 加长时间到15秒,时间太长也麻烦，
        # 会导致一开始提交就成交的那部分订单不会存到 local orders
        # 需要换地方，order_book_manager更新不及时的情况下，会导致返回的订单数据不全，或者订单里的参数默认为0的情况
        # self.local_orders = self.order_book_manager.get_order_book().orders

    @staticmethod
    def amount_disguise():
        rand = [0.8, 0.84, 0.88, 0.92, 0.95, 0.99, 1.03, 1.06, 1.09, 1.12, 1.16, 1.2]
        return rand[random.randint(0,11)]

    # suffix unique amount number to identify different buy/sell order pairs for result performance statics
    @staticmethod
    def suffix_amount_identify():
        return round(random.random()/100.0, 10)
        
    def shutdown(self):
        self.order_book_manager.cancel_all_orders(final_wait_time=30)

    def pair(self):
        return self.arguments.pair.upper()

    def token_sell(self) -> str:
        return self.arguments.pair.split('_')[0].upper()

    def token_buy(self) -> str:
        return self.arguments.pair.split('_')[1].upper()

    def our_available_balance(self, our_balances: list, token: str) -> Wad:
        return Wad.from_number(next(filter(lambda coin: coin['symbol'] == token, our_balances))['balance'])

    def our_sell_orders(self, our_orders: list) -> list:
        return list(filter(lambda order: order.is_sell, our_orders))

    def our_buy_orders(self, our_orders: list) -> list:
        return list(filter(lambda order: not order.is_sell, our_orders))
    
    def count_sell_orders(self, our_orders: list) -> int:
        return len(list(filter(lambda order: order.is_sell, our_orders)))

    def count_buy_orders(self, our_orders: list) -> int:
        return len(list(filter(lambda order: not order.is_sell, our_orders)))
    
    def synchronize_orders(self):
        # bands = Bands.read(self.bands_config, self.spread_feed, self.history)
        order_book = self.order_book_manager.get_order_book()
        # print(type(self.local_orders))
        # print(self.local_orders)
        # print(type(order_book.orders))
        # print(order_book.orders)

        self.logger.info("---**---The length of local_orders " + str(self.local_orders.__len__()))
        self.logger.info("---**---The length of order_book.orders " + str(len(order_book.orders)))
        
        local_order_ids = set(order.order_id for order in self.local_orders)
        order_book_ids = set(order.order_id for order in order_book.orders)

        self.order_book_manager.get_order_book()
        completed_order_ids = list(local_order_ids - order_book_ids)
        
        # 如果没有后续的更新 local orders，只有这里的更新模块，肯定有问题的，因为一旦有成交，
        # completed_order_ids 不为0，则永远更新不了local orders 了
        # return if there none order be completed
        # 下面这种情况，只有在order_book订单完全"包含"local_order订单时，但是两者并不相等时，才会让本地订单等于远程订单；
        # 这种一般是远程订单比本地订单多，往往比如人工在系统提交了新的订单
        if completed_order_ids.__len__() == 0:
            if local_order_ids.__len__() != order_book_ids.__len__():
                self.logger.info("---**---update local order")
                self.local_orders = order_book.orders
            return
            
        # completed_orders = list(filter(lambda order: order.order_id in completed_order_ids, self.local_orders))
        completed_orders = [order for order in self.local_orders if order.order_id in completed_order_ids]

        # completed_orders = list(filter(lambda order: order.order_id in local_order_ids, order_book.orders))
        self.logger.info("---**---The lenght of completed orders " + str(len(completed_orders)))
        self.logger.info(completed_orders)

        # completed_orders_new = list(set(self.local_orders) - set(order_book.orders))
        # print("---**---The lenght of completed new orders " + str(len(completed_orders_new)))
        # print(completed_orders_new)
        
        # completed_orders = [{'amount': Wad(2220000000000000000),
        #              'amount_symbol': 'BIX',
        #              'created_at': 1528203670000,
        #              'is_sell': True,
        #              'money': Wad(52779250000000000),
        #              'money_symbol': 'ETH',
        #              'order_id': 606026215,
        #              'price': Wad(2294750000000000)}, {'amount': Wad(2990000000000000000),
        #              'amount_symbol': 'BIX',
        #              'created_at': 1528203670000,
        #              'is_sell': False,
        #              'money': Wad(55779250000000000),
        #              'money_symbol': 'ETH',
        #              'order_id': 606026215,
        #              'price': Wad(2394750000000000)}]
        
        # our_buy_orders = self.our_buy_orders(order_book.orders)
        # our_sell_orders = self.our_sell_orders(order_book.orders)
        # print(our_buy_orders)
        # print(our_sell_orders)
        # Do not place new orders if order book state is not confirmed
        if order_book.orders_being_placed or order_book.orders_being_cancelled:
            self.logger.debug("Order book is in progress, not placing new orders")
            return
        
        # if (self.local_orders.__len__() - len(order_book.orders) > 0):
        if len(completed_orders) > 0:
                self.logger.info("--------- some orders have been done --------")
                new_orders = []
                step = 1
                count_sell_order = self.count_sell_orders(order_book.orders)
                count_buy_order = self.count_buy_orders(order_book.orders)
                
                for cod in completed_orders:
                    # print(type(cod))
                    # print(cod.is_sell)
                    # the completed order is sell order, buy order should be placed

                    self.orderlog.logger.info(" - " + str(cod.is_sell) + " - " +  str(cod.price) +
                                              " - " + str(cod.amount) + " - " + str(cod.price * cod.amount))
                    if cod.is_sell:
                        # place buy order, pay attention to rotate bix - eth
                        price = float(cod.price) * (1 - self.arbitrage_percent)
                        self.logger.info("----to submit a new buy order with price " + str(price))
                        pay_amount = float(cod.amount ) * price  # eth money 25
                        buy_amount = float(cod.amount)  # bix amount 0.05
                        new_orders.append(
                            NewOrder(is_sell=False, price=Wad.from_number(price),
                                     pay_amount=Wad.from_number(pay_amount),
                                     buy_amount=Wad.from_number(buy_amount),
                                     confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))
                        # 以当前价格为基数，重新submit一个高价格的 sell 订单，补充 sell list
                        # place sell a new order with higher price
                        # 需要判断订单的数量是否小于band order limits，并且按照差异补充订单
                        # count_sell_order = self.count_sell_orders(order_book.orders)
                        band_sell_order_gap = self.band_order_limit - count_sell_order
                        self.logger.info("---sell band gap---- " + str(band_sell_order_gap))
                        # while band_sell_order_gap > 0: # 外部已经有循环了，不需要这个循环了,否则在多订单被吃时，会加倍补充
                        # 这里只需要判断，控制数量就够了
                        if band_sell_order_gap > 0:
                            current_price = self.bibox_api.get_last_price(self.pair())
                            self.logger.info("------current price---- " + str(current_price))
                            price = float(current_price) * (1 + self.arbitrage_percent * (step + count_sell_order))
                            self.logger.info("----higher price to sell--- " + str(price))
                            pay_amount = self.each_order_amount * self.amount_disguise()  # bix amount
                            pay_amount = pay_amount + self.suffix_amount_identify() # add unique identify
                            buy_amount = pay_amount * price  # eth money
                            new_orders.append(
                                NewOrder(is_sell=True, price=Wad.from_number(price), pay_amount=Wad.from_number(pay_amount),
                                         buy_amount=Wad.from_number(buy_amount),
                                         confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))
                            # step = step + 1
                            # band_sell_order_gap = band_sell_order_gap - 1
                            count_sell_order = count_sell_order + 1
        
                    else:  # buy order had been completed
                        # to place a sell order
                        price = float(cod.price) * (1 + self.arbitrage_percent)
                        self.logger.info("----price--- sell--- " + str(price))
                        pay_amount = float(cod.amount)  # bix amount
                        buy_amount = pay_amount * price  # eth money
                        new_orders.append(
                            NewOrder(is_sell=True, price=Wad.from_number(price), pay_amount=Wad.from_number(pay_amount),
                                     buy_amount=Wad.from_number(buy_amount),
                                     confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))
                        # 以当前价格为基数，重新submit一个 buy 订单，补充 buy list
                        # 需要判断订单的数量是否小于band order limits，并且按照差异补充订单
                        # count_buy_order = self.count_buy_orders(order_book.orders)
                        band_buy_order_gap = self.band_order_limit - count_buy_order
                        self.logger.info("---buy band gap----" + str(band_buy_order_gap))
                        
                        # while band_buy_order_gap > 0:
                        if band_buy_order_gap > 0:
                            #基础价格放在循环里的话，能快速反映当前价格，特保是激烈波动的时候；但是增加了请求次数
                            current_price = self.bibox_api.get_last_price(self.pair())
                            price = float(current_price) * (1 - self.arbitrage_percent * (step + count_buy_order))
                            self.logger.info("----lower price order to buy--- " + str(price))
                            tmp = self.each_order_amount * self.amount_disguise()
                            pay_amount = tmp * price  # eth money 25
                            buy_amount = tmp  # bix amount 0.05
                            buy_amount = buy_amount + self.suffix_amount_identify()  # add unique identify
                            new_orders.append(
                                NewOrder(is_sell=False, price=Wad.from_number(price),
                                         pay_amount=Wad.from_number(pay_amount),
                                         buy_amount=Wad.from_number(buy_amount),
                                         confirm_function=lambda: self.sell_limits.use_limit(time.time(), pay_amount)))

                            # band_buy_order_gap = band_buy_order_gap - 1
                            count_buy_order = count_buy_order + 1
                        step = step + 1

                self.place_orders(new_orders)
        
                # update local orders, 前面有更新模块，与这边不完全相同，尤其是有成交的情况下，必须要更新
                # 是这样吗？ 似乎也不是的，有成交的情况下，下一次订单也会让 set（local） - set（order book）=0的，集合相减的特殊之处
                # 如果这样就没有必要了。
                # 是这样简单的复制更新，还是本地自己维护一个 id list 好呢？ 也就是把（1）确定成交的从 local 删除；
                # （2）确定提交的add 到本地；
                # 缩进到循环： if len(completed_orders) > 0：，在出现两者不一致的时候，同步更新订单；
                # 但是这个会导致一个问题，就是初始化的订单里，有price 为0，导致两者不一致的情况，怎么办？这里解决了，是通过 order id对比而不是
                # 直接的 order 对比，所以应该是解决了才对
                self.logger.info("-----update local order------")
                self.local_orders = self.order_book_manager.get_order_book().orders
        
                # Cancel orders
                # cancellable_orders = bands.cancellable_orders(our_buy_orders=self.our_buy_orders(order_book.orders),
                #                                               our_sell_orders=self.our_sell_orders(order_book.orders),
                #                                               target_price=target_price)
    
    
                # if len(cancellable_orders) > 0:
                # self.order_book_manager.cancel_orders(cancellable_orders)
                # print("there is " + str(len(cancellable_orders)) + " orders should be cancelled")
    
    
                # Place new orders
        # self.place_orders(bands.new_orders(our_buy_orders=self.our_buy_orders(order_book.orders),
        #                                    our_sell_orders=self.our_sell_orders(order_book.orders),
        #                                    our_buy_balance=self.our_available_balance(order_book.balances, self.token_buy()),
        #                                    our_sell_balance=self.our_available_balance(order_book.balances, self.token_sell()),
        #                                    target_price=target_price)[0])

    def place_orders(self, new_orders):
        def place_order_function(new_order_to_be_placed):
            amount = new_order_to_be_placed.pay_amount if new_order_to_be_placed.is_sell else new_order_to_be_placed.buy_amount
            amount_symbol = self.token_sell()
            money = new_order_to_be_placed.buy_amount if new_order_to_be_placed.is_sell else new_order_to_be_placed.pay_amount
            money_symbol = self.token_buy()
            
            new_order_id = self.bibox_api.place_order(is_sell=new_order_to_be_placed.is_sell,
                                                      amount=amount,
                                                      amount_symbol=amount_symbol,
                                                      money=money,
                                                      money_symbol=money_symbol)

            return Order(new_order_id, 0, new_order_to_be_placed.is_sell, Wad(money/amount), amount, amount_symbol, money, money_symbol)

        for new_order in new_orders:
            self.order_book_manager.place_order(lambda new_order=new_order: place_order_function(new_order))
            
            
    def get_price(self, pair):
        self.bibox_api.get_all_trades()
    


if __name__ == '__main__':
    BiboxMarketSurfer(sys.argv[1:]).main()
