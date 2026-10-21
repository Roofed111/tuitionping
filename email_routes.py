"""Public email preferences and protected email-list/campaign administration."""
from fastapi import Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
import email_engagement as emails


def register(app, templates, require_admin, sender, email_active):
    def page(request, template, data=None, status=200):
        return templates.TemplateResponse(request, template, {'request': request, **(data or {})},
            status_code=status, headers={'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex', 'Referrer-Policy': 'no-referrer'})

    @app.get('/email-kit', response_class=HTMLResponse)
    def email_kit(request: Request):
        return page(request, 'email_kit.html')

    @app.post('/email-list/join')
    def join(request: Request, email: str = Form(...), name: str = Form(''),
             marketing_optin: str = Form(''), website: str = Form(''),
             source: str = Form('/email-kit')):
        if source not in ('/email-kit', '/guides', '/demo', '/tools/tuition-payment-tracker', '/guides/tuition-reminder-templates', '/'):
            source = '/email-kit'
        if website:
            return page(request, 'email_kit.html', {'sent': True})
        ip = request.headers.get('x-forwarded-for', request.client.host if request.client else '').split(',')[0].strip()
        if not emails.rate_allowed(ip):
            return page(request, 'email_kit.html', {'error': 'Too many requests. Try again later, or download the kit directly.'}, 429)
        if not email_active():
            return page(request, 'email_kit.html', {'error': 'Email delivery is temporarily unavailable. You can still download the complete kit directly.'}, 503)
        try:
            oid = emails.request_email(email, name, source, marketing_optin == 'on')
        except ValueError as exc:
            return page(request, 'email_kit.html', {'error': str(exc)}, 400)
        if oid:
            emails.deliver_due(sender, True, limit=1, only_id=oid)
        # The same response for new, existing and suppressed addresses.
        return page(request, 'email_kit.html', {'sent': True})

    @app.get('/email-list/confirm', response_class=HTMLResponse)
    def confirm_form(request: Request, token: str = ''):
        return page(request, 'email_preferences.html', {'mode': 'confirm', 'token': token})

    @app.post('/email-list/confirm')
    def confirm(request: Request, token: str = Form('')):
        ok = emails.confirm_contact(token)
        return page(request, 'email_preferences.html', {'mode': 'confirmed' if ok else 'invalid'})

    @app.get('/email-list/preferences', response_class=HTMLResponse)
    def preferences(request: Request, token: str = ''):
        c = emails.by_token(token)
        return page(request, 'email_preferences.html', {'mode': 'preferences' if c else 'invalid', 'token': token})

    @app.post('/email-list/preferences')
    def opt_out(request: Request, token: str = ''):
        # RFC 8058 one-click POST and the visible form share the capability URL.
        # No login, cookie, email entry or CSRF token is needed to stop emails.
        ok = emails.unsubscribe(token)
        return page(request, 'email_preferences.html', {'mode': 'unsubscribed' if ok else 'invalid'})

    @app.get('/admin/email', response_class=HTMLResponse)
    def admin_email(request: Request):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        return page(request, 'admin_email.html', {'report': emails.report(), 'email_active': email_active()})

    @app.post('/admin/email/settings')
    def save_settings(request: Request, postal_address: str = Form(''), followups_enabled: str = Form('')):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        try:
            emails.save_settings(postal_address, followups_enabled == 'on')
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse('/admin/email?saved=1', status_code=303)

    @app.get('/admin/email/export')
    def export(request: Request):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        return Response(emails.export_csv(), media_type='text/csv', headers={
            'Content-Disposition': 'attachment; filename="tuitionping-confirmed-marketing-subscribers.csv"',
            'Cache-Control': 'private, no-store', 'X-Robots-Tag': 'noindex'})

    @app.post('/admin/email/suppress')
    def suppress(request: Request, contact_id: int = Form(...)):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        emails.suppress(contact_id)
        return RedirectResponse('/admin/email', status_code=303)

    @app.post('/admin/email/campaigns')
    def draft(request: Request, subject: str = Form(...), body: str = Form(...), cta_path: str = Form('/demo')):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        try:
            cid = emails.create_campaign(subject, body, cta_path)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse(f'/admin/email/campaigns/{cid}', status_code=303)

    @app.get('/admin/email/campaigns/{cid}', response_class=HTMLResponse)
    def preview(request: Request, cid: int):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        camp = emails.campaign(cid)
        if not camp:
            return HTMLResponse('Campaign not found.', status_code=404)
        report = emails.report()
        sample = {'manage_token': 'preview-not-a-live-unsubscribe-token'}
        html, _ = emails.message(emails.campaign_content(camp), sample, promotional=True)
        html = html.split('<body>', 1)[1].split('</body>', 1)[0]
        return page(request, 'admin_email_preview.html', {'campaign': camp, 'preview_html': html,
                    'confirmed': report['confirmed'], 'settings': report['settings'], 'email_active': email_active()})

    @app.post('/admin/email/campaigns/{cid}/send')
    def send_campaign(request: Request, cid: int, expected_audience: int = Form(...), reviewed: str = Form('')):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        camp = emails.campaign(cid)
        if not camp:
            return HTMLResponse('Campaign not found.', status_code=404)
        if camp['status'] != 'draft':
            return RedirectResponse(f'/admin/email/campaigns/{cid}', status_code=303)
        if not email_active():
            return HTMLResponse('Email sending is not configured.', status_code=503)
        if reviewed != 'on':
            return HTMLResponse('Review the message and confirm before sending.', status_code=400)
        if expected_audience != emails.report()['confirmed']:
            return HTMLResponse('The audience changed. Reload the preview before sending.', status_code=409)
        try:
            emails.queue_campaign(cid, expected_audience=expected_audience)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=400)
        return RedirectResponse(f'/admin/email/campaigns/{cid}', status_code=303)

    @app.post('/admin/email/campaigns/{cid}/cancel')
    def cancel(request: Request, cid: int):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        emails.cancel_campaign(cid)
        return RedirectResponse('/admin/email', status_code=303)

    @app.post('/admin/email/process')
    def process(request: Request):
        _, redirect = require_admin(request)
        if redirect:
            return redirect
        result = emails.run(sender, email_active())
        return RedirectResponse('/admin/email?processed=' + str(result['accepted']), status_code=303)
