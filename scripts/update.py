"""Refresh public source data; fail closed per series and retain last valid value."""
import csv, io, json, math, re, subprocess, tempfile, unicodedata
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urljoin

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime.now(timezone.utc)
TODAY=NOW.date()
STAMP=NOW.isoformat(timespec='seconds')

def get(url):
    with urlopen(Request(url,headers={'User-Agent':'USDJPY-public-data/1.0'}),timeout=30) as r:
        data=r.read(8_000_001)
        if len(data)>8_000_000: raise ValueError('response too large')
        return data

def html(url): return get(url).decode('utf-8-sig')
def text(s): return re.sub(r'\s+',' ',unescape(re.sub('<[^>]+>',' ',s)))
def pdf(url):
    with tempfile.TemporaryDirectory() as d:
        p=Path(d)/'source.pdf';p.write_bytes(get(url))
        return subprocess.check_output(['pdftotext','-layout',str(p),'-'],timeout=20).decode()

def latest_link(page,pattern):
    links=[]
    for href in re.findall(r'href=["\']([^"\']+)',html(page)):
        m=re.search(pattern,href)
        if m: links.append((m.group(1),urljoin(page,href)))
    if not links: raise ValueError('source link not found')
    return max(links)

def fx():
    d,url=latest_link('https://www.boj.or.jp/en/statistics/market/forex/fxdaily/index.htm',r'fx(\d{6})\.pdf')
    s=pdf(url)
    m=re.search(r'(?:At\s*)?17:00\s*JST\s*(\d{2,3}\.\d+)\s*[-–－]\s*(\d+(?:\.\d+)?)',s)
    if not m: # Some documents place the numeric row before its English caption.
        m=re.search(r'17:00[^\n]*?\s(\d{2,3}\.\d+)\s*[-–－]\s*(\d+(?:\.\d+)?)',s)
    if not m: raise ValueError('17:00 USDJPY quote missing')
    lo=float(m[1]);hi=float(m[2]) if '.' in m[2] else math.floor(lo)+int(m[2])/100
    if hi<lo: hi+=1
    if not 0<=hi-lo<=.2: raise ValueError('invalid spread')
    return {'value':round((lo+hi)/2,5),'observed_at':datetime.strptime(d,'%y%m%d').date().isoformat(),'unit':'JPY/USD','source_url':url,'label':'日銀 17:00 JST 売買気配の中値'}

def fed():
    from fractions import Fraction
    d,url=latest_link('https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm',r'monetary(\d{8})a\.htm')
    source=text(html(url)).replace('–','-').replace('−','-')
    m=re.search(r'target range for the federal funds rate (?:at|to)\s+([\d./ -]+?)\s+to\s+([\d./ -]+?)\s+percent',source,re.I)
    if not m: raise ValueError('FOMC target range missing')
    def number(s): return sum(float(Fraction(x)) for x in s.strip().replace('-',' ').split())
    lo,hi=number(m[1]),number(m[2])
    if not 0<=hi-lo<=1: raise ValueError('invalid FOMC target range')
    return {'value':lo,'upper':hi,'observed_at':datetime.strptime(d,'%Y%m%d').date().isoformat(),'unit':'%','source_url':url,'label':'FRB FOMC政策金利・決定日'}

def boj():
    found=[]
    for year in [TODAY.year,TODAY.year-1]:
        try: found.append(latest_link(f'https://www.boj.or.jp/en/mopo/mpmdeci/state_{year}/index.htm',r'k(\d{6})a\.pdf'))
        except Exception:
            if year==TODAY.year-1 and not found: raise
        if found: break
    d,url=max(found);s=text(pdf(url))
    m=re.search(r'overnight call rate to remain at around\s*(-?\d+(?:\.\d+)?)\s*percent',s,re.I)
    if not m: raise ValueError('policy guideline not recognized')
    return {'value':float(m[1]),'observed_at':datetime.strptime(d,'%y%m%d').date().isoformat(),'unit':'%','source_url':url,'label':'日銀 政策金利・決定日'}

def yen_number(s):
    s=unicodedata.normalize('NFKC',s).replace(',','').replace(' ','')
    negative=any(c in s for c in ['▲','△','−','-'])
    m=re.search(r'(?:(\d+)兆)?(?:(\d+)億)?円',s)
    if not m or not any(m.groups()): raise ValueError('unrecognized yen amount')
    return (-1 if negative else 1)*(int(m[1] or 0)+int(m[2] or 0)/10000)

def bop():
    d,url=latest_link('https://www.mof.go.jp/policy/international_policy/reference/balance_of_payments/release_date.htm',r'pg(\d{6})\.htm')
    source=html(url); result={}
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>',source,re.S|re.I):
        cells=[text(c).strip() for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>',row,re.S|re.I)]
        for label,key in [('貿易収支','trade'),('サービス収支','services'),('第一次所得収支','income')]:
            if label in cells and key not in result:
                j=cells.index(label)
                if j+1<len(cells): result[key]=yen_number(cells[j+1])
    if set(result)!={'trade','services','income'}: raise ValueError('monthly balance table missing')
    return {'value':result['trade'],'services':result['services'],'income':result['income'],'observed_at':d[:4]+'-'+d[4:]+'-01','period':d[:4]+'-'+d[4:],'unit':'trillion JPY/month','source_url':url,'label':'財務省 国際収支・月次速報（非季調）'}

def validate(key,item,old):
    date=datetime.fromisoformat(item['observed_at']).date()
    if date>TODAY: raise ValueError('future observation')
    if old and item['observed_at']<old['observed_at']: raise ValueError('older than saved observation')
    bounds={'fx':(30,400),'fed':(-2,25),'boj':(-2,25),'bop':(-30,30)}
    lo,hi=bounds[key]
    for field in ['value','upper','services','income']:
        if field in item and (not math.isfinite(item[field]) or not lo<=item[field]<=hi): raise ValueError('value outside validation bounds')
    if key=='fx' and old:
        days=max(1,(date-datetime.fromisoformat(old['observed_at']).date()).days)
        if abs(math.log(item['value']/old['value']))>min(.30,.08*math.sqrt(days)): raise ValueError('large FX jump; review required')
    return item

def main():
    p=ROOT/'data/latest.json'
    data=json.loads(p.read_text()) if p.exists() else {'schema_version':1,'series':{}}
    failed=[]
    for key,fn in [('fx',fx),('fed',fed),('boj',boj),('bop',bop)]:
        old=data['series'].get(key)
        try:
            item=validate(key,fn(),old);item.update(checked_at=STAMP,last_success_at=STAMP,status='ok')
            data['series'][key]=item
            data.get('unavailable',{}).pop(key,None)
            print(key+': OK')
        except Exception as e:
            failed.append(key)
            if old: old.update(checked_at=STAMP,status='error',error=type(e).__name__+': '+str(e)[:160])
            else: data.setdefault('unavailable',{})[key]={'checked_at':STAMP,'status':'error'}
            print('::warning::'+key+' update failed; previous value retained: '+str(e)[:160])
    data.update(checked_at=STAMP,failed=failed)
    p.parent.mkdir(exist_ok=True);tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n');tmp.replace(p)
    return bool(failed)

if __name__=='__main__': raise SystemExit(main())
