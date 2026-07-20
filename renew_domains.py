import requests
import os
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

# 从环境变量获取配置
API_KEY = os.environ.get('DNSHE_API_KEY')
API_SECRET = os.environ.get('DNSHE_API_SECRET')
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = os.environ.get('SMTP_PORT') or '465'
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
SMTP_FROM = os.environ.get('SMTP_FROM') or SMTP_USER
SMTP_TO = os.environ.get('SMTP_TO')

BASE_URL = "https://api005.dnshe.com/index.php?m=domain_hub"

# 续期阈值：到期时间小于该天数则执行续期
RENEW_THRESHOLD_DAYS = 180

def _get_bool_env(name, default):
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def send_smtp(content):
    if not SMTP_HOST or not SMTP_TO:
        print("未配置 SMTP_HOST 或 SMTP_TO，跳过邮件推送")
        return

    recipients = [address.strip() for address in SMTP_TO.split(',') if address.strip()]
    if not recipients:
        print("SMTP_TO 未包含有效的收件人，跳过邮件推送")
        return
    if not SMTP_FROM:
        print("未配置 SMTP_FROM 或 SMTP_USER，跳过邮件推送")
        return

    try:
        port = int(SMTP_PORT)
        use_ssl = _get_bool_env('SMTP_USE_SSL', port == 465)
        use_starttls = _get_bool_env('SMTP_USE_STARTTLS', not use_ssl)
        if use_ssl and use_starttls:
            raise ValueError("SMTP_USE_SSL 和 SMTP_USE_STARTTLS 不能同时启用")
        if bool(SMTP_USER) != bool(SMTP_PASSWORD):
            raise ValueError("SMTP_USER 和 SMTP_PASSWORD 必须同时配置")

        message = EmailMessage()
        message['Subject'] = 'DNSHE 域名自动续期报告'
        message['From'] = SMTP_FROM
        message['To'] = ', '.join(recipients)
        message.set_content(content)

        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(SMTP_HOST, port, timeout=30) as server:
            if use_starttls:
                server.starttls(context=ssl.create_default_context())
            if SMTP_USER:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message, from_addr=SMTP_FROM, to_addrs=recipients)
        print("SMTP 邮件推送成功")
    except Exception as e:
        print(f"SMTP 邮件推送失败: {str(e)}")

def main():
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET,
        "Content-Type": "application/json"
    }

    # 1. 获取所有子域名（显式请求到期时间字段）
    list_url = f"{BASE_URL}&endpoint=subdomains&action=list&fields=id,subdomain,rootdomain,full_domain,status,expires_at,never_expires"
    try:
        resp = requests.get(list_url, headers=headers)
        subdomains = resp.json().get('subdomains', [])
    except Exception as e:
        send_smtp(f"获取域名列表失败: {str(e)}")
        return

    today = datetime.now()
    renewal_results = []  # 第一段：本次续期结果
    expiry_info = []      # 第二段：所有域名到期时间

    # 2. 遍历域名，检查到期时间并选择性续期
    for domain in subdomains:
        domain_id = domain['id']
        full_domain = domain['full_domain']
        expires_at_str = domain.get('expires_at')
        never_expires = domain.get('never_expires', 0)

        # 检查是否为永不过期域名
        if never_expires:
            expiry_info.append(f"{full_domain}: 到期时间 永久有效")
            renewal_results.append(f"⏭️ {full_domain}: 已设置为永不过期，跳过续期")
            continue

        # 计算剩余天数
        expires_at = None
        if expires_at_str:
            expires_at = datetime.strptime(expires_at_str, '%Y-%m-%d %H:%M:%S')
            days_remaining = (expires_at - today).days
        else:
            days_remaining = None

        # 记录到期信息（第二段用）
        if days_remaining is not None:
            expiry_info.append(f"{full_domain}: 到期时间 {expires_at_str} (剩余 {days_remaining}天)")
        else:
            expiry_info.append(f"{full_domain}: 到期时间 未知")

        # 判断是否需要续期：剩余天数 < 180天 才续期
        if days_remaining is not None and days_remaining >= RENEW_THRESHOLD_DAYS:
            renewal_results.append(f"⏭️ {full_domain}: 剩余 {days_remaining}天 >= {RENEW_THRESHOLD_DAYS}天，跳过续期")
            continue

        # 执行续期
        renew_url = f"{BASE_URL}&endpoint=subdomains&action=renew"
        payload = {"subdomain_id": domain_id}

        try:
            r_resp = requests.post(renew_url, headers=headers, json=payload).json()
            if r_resp.get('success'):
                new_expiry = r_resp.get('new_expires_at', '未知')
                charged = r_resp.get('charged_amount', 0)
                renewal_results.append(f"✅ {full_domain}: 续期成功 (新到期: {new_expiry}, 消耗: {charged}积分)")
            else:
                msg = r_resp.get('message', '未知错误')
                renewal_results.append(f"❌ {full_domain}: 续期失败 ({msg})")
        except Exception as e:
            renewal_results.append(f"❌ {full_domain}: 请求异常 ({str(e)})")

    # 3. 构建两段式通知消息
    message_parts = []
    message_parts.append("=== 本次续期结果 ===")
    if renewal_results:
        message_parts.extend(renewal_results)
    else:
        message_parts.append("（所有域名剩余天数 >= 180天，本次无需续期）")

    message_parts.append("")
    message_parts.append("=== 所有域名到期时间 ===")
    message_parts.extend(expiry_info)

    message = "\n".join(message_parts)
    print(message)
    send_smtp(message)

if __name__ == "__main__":
    main()
