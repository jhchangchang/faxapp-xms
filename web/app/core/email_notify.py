"""
email_notify.py - 이메일 알림 발송 (SMTP)

발송 실패·수신 시 관리자에게 이메일 통보.
설정은 notify_config 테이블에서 읽음. stdlib(smtplib)만 사용.
실패해도 예외를 던지지 않음 (앱 흐름 방해 금지).
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.header import Header
from . import db

logger = logging.getLogger('faxapp.email')


def _get_cfg():
    """이메일 설정 조회. 없거나 비활성이면 None."""
    try:
        cfg = db.query_one(
            "SELECT email_enabled, smtp_host, smtp_port, smtp_user, smtp_pass, "
            "smtp_tls, email_from, email_to FROM notify_config WHERE id=1")
        if not cfg or not cfg.get('email_enabled'):
            return None
        if not cfg.get('smtp_host') or not cfg.get('email_to'):
            return None
        return cfg
    except Exception:
        return None


def send_email(subject, body):
    """이메일 발송. 성공 True / 실패·미설정 False. 예외 안 던짐."""
    cfg = _get_cfg()
    if not cfg:
        return False
    try:
        recipients = [x.strip() for x in (cfg['email_to'] or '').split(',') if x.strip()]
        if not recipients:
            return False
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = cfg.get('email_from') or cfg.get('smtp_user') or 'faxapp'
        msg['To'] = ', '.join(recipients)

        host = cfg['smtp_host']
        port = int(cfg.get('smtp_port') or 587)
        timeout = 10
        if cfg.get('smtp_tls'):
            server = smtplib.SMTP(host, port, timeout=timeout)
            server.starttls()
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        if cfg.get('smtp_user'):
            server.login(cfg['smtp_user'], cfg.get('smtp_pass') or '')
        server.sendmail(msg['From'], recipients, msg.as_string())
        server.quit()
        logger.info(f'이메일 알림 발송: {subject} → {len(recipients)}명')
        return True
    except Exception as e:
        logger.warning(f'이메일 알림 실패(무시): {e}')
        return False


def notify_fail_email(to_number, reason):
    """발송 실패 이메일."""
    return send_email(
        f'[FaxApp] 팩스 발송 실패 - {to_number}',
        f'팩스 발송이 실패했습니다.\n\n수신번호: {to_number}\n사유: {reason}\n\n'
        f'FaxApp 관리자 화면에서 확인하세요.')


def notify_recv_email(from_number, pages):
    """수신 이메일."""
    return send_email(
        f'[FaxApp] 새 팩스 수신 - {from_number}',
        f'새 팩스를 수신했습니다.\n\n발신번호: {from_number}\n페이지: {pages}p\n\n'
        f'FaxApp 수신함에서 확인하세요.')


def test_email():
    """설정 테스트용 발송."""
    return send_email('[FaxApp] 이메일 알림 테스트',
                      'FaxApp 이메일 알림이 정상 설정되었습니다.')
