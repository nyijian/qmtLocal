#coding:gbk

def init(ContextInfo):
    ContextInfo.stock = ContextInfo.stockcode + '.' + ContextInfo.market
    price = ContextInfo.get_market_data_ex(['open','high','low','close'], [ContextInfo.stock], 
                                            start_time='', end_time='', period='1d', subscribe=False)
    print(price[ContextInfo.stock].head())

def handlebar(ContextInfo):
    bar_timetag = ContextInfo.get_bar_timetag(ContextInfo.barpos)
    bar_date = timetag_to_datetime(bar_timetag, '%Y%m%d%H%M%S')
    print('获取截至到%s为止前5根k线的开高低收等字段:'%(bar_date))
    
    price = ContextInfo.get_market_data_ex(
        [], [ContextInfo.stock], 
        end_time=bar_date,
        period=ContextInfo.period, 
        subscribe=True, 
        count=5,
    )

    print(price[ContextInfo.stock].to_dict('dict'))
