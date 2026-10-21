"""Public calculator distribution and solution selection; existing growth tracking applies."""
import re
from html import escape
from urllib.parse import urlencode
from fastapi import Request
from fastapi.responses import HTMLResponse
import billing
import partner_resources

EMBED_PATH = '/embed/late-fee-calculator'
EMBED_GUIDE = '/partners/embed-calculator'
FINDER_PATH = '/find-your-tuition-solution'
BASE = 'https://www.tuitionping.com'

def destinations(code=''):
    code = code if re.fullmatch(r'[a-z0-9][a-z0-9-]{2,47}', code or '') else ''
    def link(path):
        if code:
            # Registration is checked when the visitor clicks through; the
            # iframe itself neither looks up partners nor creates a visitor.
            return partner_resources.link(code, path)
        return BASE + path + '?' + urlencode({'utm_source':'embedded-calculator', 'utm_medium':'referral', 'utm_campaign':'calculator-embed'})
    return {'calculator_url':link('/tools/late-fee-calculator'), 'finder_url':link(FINDER_PATH)}

def embed_markup(code=''):
    src = BASE + EMBED_PATH + ('?' + urlencode({'partner':code}) if code else '')
    return ('<iframe src="' + escape(src, quote=True) + '" title="Daycare tuition late-fee calculator" '
            'width="100%" height="1120" loading="lazy" referrerpolicy="no-referrer" '
            'sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox" '
            'style="display:block;border:0;max-width:760px;"></iframe>')

def register(app, templates, current_provider):
    @app.get(EMBED_PATH, response_class=HTMLResponse)
    def embedded_calculator(request: Request, partner: str = ''):
        response = templates.TemplateResponse(request, 'embedded_calculator.html', {'request':request, **destinations(partner)})
        response.headers.update({'X-Robots-Tag':'noindex', 'Cache-Control':'public, max-age=3600'})
        return response

    @app.get(EMBED_GUIDE, response_class=HTMLResponse)
    def calculator_distribution(request: Request, partner: str = ''):
        selected = partner_resources.get_partner(partner) if partner else None
        return templates.TemplateResponse(request, 'embed_calculator_guide.html', {
            'request':request, 'provider':current_provider(request), 'partner':selected,
            'invalid_partner':bool(partner and not selected),
            'snippet':embed_markup(selected['code'] if selected else ''),
        })

    @app.get(FINDER_PATH, response_class=HTMLResponse)
    def solution_finder(request: Request):
        catalog = [{'key':key, **billing.PLANS[key], 'locations':billing.plan_location_limit(key)} for key in billing.PLAN_ORDER]
        return templates.TemplateResponse(request, 'solution_finder.html', {
            'request':request, 'provider':current_provider(request), 'catalog':catalog, 'trial_days':billing.TRIAL_DAYS,
        })
