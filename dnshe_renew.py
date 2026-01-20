import requests
import json
import os

# 从环境变量获取配置
API_KEY = os.environ.get('DNSHE_API_KEY')
API_SECRET = os.environ.get('DNSHE_API_SECRET')
PUSHPLUS_TOKEN = os.environ.get('PUSHPLUS_TOKEN')
PUSHPLUS_TOPIC = os.environ.get('PUSHPLUS_TOPIC')  # 群组编码

BASE_URL = "https://api005.dnshe.com/index.php?m=domain_hub"

def send_pushplus(content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PushPlus Token，跳过推送")
        return
    
    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": "DNSHE 域名自动续期报告",
        "content": content,
        "template": "txt",
        "topic": PUSHPLUS_TOPIC
    }
    requests.post(url, json=data)

def main():
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET,
        "Content-Type": "application/json"
    }

    # 1. 获取所有子域名 
    list_url = f"{BASE_URL}&endpoint=subdomains&action=list"
    try:
        resp = requests.get(list_url, headers=headers)
        subdomains = resp.json().get('subdomains', [])
    except Exception as e:
        send_pushplus(f"获取域名列表失败: {str(e)}")
        return

    results = []
    
    # 2. 遍历并续期 
    for domain in subdomains:
        domain_id = domain['id']
        full_domain = domain['full_domain']
        
        renew_url = f"{BASE_URL}&endpoint=subdomains&action=renew"
        payload = {"subdomain_id": domain_id}
        
        try:
            r_resp = requests.post(renew_url, headers=headers, json=payload).json()
            if r_resp.get('success'):
                # 提取新过期时间 [cite: 6]
                new_expiry = r_resp.get('new_expires_at', '未知')
                results.append(f"✅ {full_domain}: 续期成功 (新到期: {new_expiry})")
            else:
                results.append(f"❌ {full_domain}: 续期失败 ({r_resp.get('message', '未知错误')})")
        except Exception as e:
            results.append(f"❌ {full_domain}: 请求异常")

    # 3. 汇总消息并推送 [cite: 14]
    if results:
        message = "\n".join(results)
        print(message)
        send_pushplus(message)
    else:
        send_pushplus("未发现需要续期的域名。")

if __name__ == "__main__":
    main()
