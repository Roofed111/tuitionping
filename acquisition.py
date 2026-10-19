"""Safe first-touch source detail. No click IDs or arbitrary query strings."""
import json
import re
from urllib.parse import urlsplit

ENGINES = ((r'(?:www\.)?google\.(?:com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})', 'google'),
           (r'(?:www\.)?bing\.com', 'bing'), (r'(?:www\.)?duckduckgo\.com', 'duckduckgo'),
           (r'(?:search\.)?yahoo\.com', 'yahoo'), (r'(?:www\.)?ecosia\.org', 'ecosia'),
           (r'(?:www\.)?search\.brave\.com', 'brave'))
SOCIAL = {'youtube.com':'YouTube', 'youtu.be':'YouTube', 'facebook.com':'Facebook',
          'instagram.com':'Instagram', 'linkedin.com':'LinkedIn', 'reddit.com':'Reddit',
          'pinterest.com':'Pinterest', 't.co':'X', 'x.com':'X'}

def public_label(value, spaces=False):
    pattern = r'[A-Za-z0-9 _.-]{1,80}' if spaces else r'[A-Za-z0-9_.-]{1,60}'
    value = (value or '').strip()
    # Campaign labels only; never emails, IDs, or phone-sized digit strings.
    return value if re.fullmatch(pattern, value) and not re.search(r'\d{6,}', value) else ''

def source_label(source, medium):
    source = source or 'direct'
    if source == 'direct': return 'Direct / source unavailable'
    if source == 'postcard': return 'Postcard QR code'
    if source == 'customer_referral': return 'Customer referral'
    engine = next((name for pattern,name in ENGINES if re.fullmatch(pattern, source)), None)
    engine = source if source in {'google','bing','duckduckgo','yahoo','ecosia','brave'} else engine
    if engine:
        return engine.title().replace('Duckduckgo','DuckDuckGo') + (' Search · organic' if medium == 'organic' else ' Ads · paid' if medium in {'cpc','ppc','paid_search'} else ' · ' + medium)
    host = source.lower()
    social = next((label for domain,label in SOCIAL.items() if host == domain or host.endswith('.'+domain)), None)
    return (social or source) + (' · social' if medium == 'social' else ' · referral' if medium == 'referral' else ' · ' + (medium or 'campaign'))

def describe(request):
    source, medium, campaign = (public_label(request.query_params.get(k,'')) for k in ('utm_source','utm_medium','utm_campaign'))
    detail = {'campaign':campaign, 'content':public_label(request.query_params.get('utm_content','')),
              'term':public_label(request.query_params.get('utm_term',''), spaces=True),
              'referrer_host':'', 'referrer_path':'', 'basis':'No external referrer or campaign label'}
    try:
        ref = urlsplit(request.headers.get('referer',''))
        host = (ref.hostname or '').lower()
        if re.fullmatch(r'[a-z0-9.-]{1,120}',host): detail['referrer_host'] = host
    except ValueError:
        ref, host = None, ''
    if request.url.path == '/postcard' or request.cookies.get('tp_src') == 'postcard':
        source,medium,detail['basis'] = 'postcard','direct_mail','Postcard route or campaign cookie'
    elif re.fullmatch(r'TP-[A-Z0-9]{6,20}',request.query_params.get('ref','').upper()):
        source,medium,detail['basis'] = 'customer_referral','referral','Referral code present'
    elif source:
        medium = medium or 'campaign'
        detail['basis'] = 'UTM campaign labels (link-supplied attribution)'
    elif request.query_params.get('gclid') or request.query_params.get('gbraid') or request.query_params.get('wbraid'):
        source,medium,detail['basis'] = 'google','cpc','Google advertising marker present; value not retained'
    elif request.query_params.get('msclkid'):
        source,medium,detail['basis'] = 'bing','cpc','Microsoft advertising marker present; value not retained'
    elif host in {'www.tuitionping.com','tuitionping.com',request.url.hostname}:
        source,medium = 'direct','none'
        detail['basis'] = 'Internal link; original external source unavailable'
    elif host:
        engine = next((name for pattern,name in ENGINES if re.fullmatch(pattern,host)),None)
        path = ref.path if ref else ''
        if engine and path in {'','/','/search','/url','/imgres'}:
            source,medium,detail['basis'] = engine,'organic','Recognized search-engine referrer'
            detail['referrer_path'] = path
        else:
            source,medium = host, ('social' if any(host == d or host.endswith('.'+d) for d in SOCIAL) else 'referral')
            detail['basis'] = 'External referring host'
    else:
        source,medium = 'direct','none'
    detail.update(source=source,medium=medium,label=source_label(source,medium))
    return detail

def display(row):
    try:
        detail = json.loads(row.get('attribution_json') or '{}')
    except (ValueError,TypeError):
        detail = {}
    if not isinstance(detail,dict): detail = {}
    return {'label':source_label(row.get('source'),row.get('medium','none')),
            'basis':detail.get('basis','Historical source label; further referral detail unavailable'),
            'referrer':detail.get('referrer_host',''), 'referrer_path':detail.get('referrer_path',''),
            'content':detail.get('content',''), 'term':detail.get('term','')}
