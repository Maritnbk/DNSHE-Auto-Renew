import requests
import os
import json

# 从环境变量获取配置
# 如果在 Secrets 中没设置 DNSHE_URL，默认尝试 api.dnshe.com
API_URL = os.getenv('DNSHE_URL', 'https://api.dnshe.com/index.php')
API_KEY = os.getenv('DNSHE_KEY')
API_SECRET = os.getenv('DNSHE_SECRET')
PUSHPLUS_TOKEN = os.getenv('PUSHPLUS_TOKEN')
PUSHPLUS_TOPIC = os.getenv('PUSHPLUS_TOPIC')

def send_pushplus(content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PushPlus Token，跳过推送。")
        return
    url = "http://www.pushplus.plus/send"
    data = {"token": PUSHPLUS_TOKEN, "title": "DNSHE 域名续期报告", "content": content, "template": "markdown", "topic": PUSHPLUS_TOPIC}
    try:
        requests.post(url, json=data, timeout=10)
    except:
        pass

def main():
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # 构建基础请求参数
    params = "m=domain_hub&endpoint=dns_records&action=list"
    full_url = f"{API_URL}?{params}"

    try:
        print(f"正在请求地址: {full_url}")
        response = requests.get(full_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            msg = f"❌ API 请求失败\n状态码: {response.status_code}\n地址: {API_URL}\n请检查 Secrets 中的 DNSHE_URL 是否正确。"
            print(msg)
            send_pushplus(msg)
            return

        res_data = response.json()
        if not res_data.get("success"):
            send_pushplus(f"❌ 获取列表失败: {res_data.get('message')}")
            return
        
        subdomains = res_data.get("subdomains", [])
        results = []

        for domain in subdomains:
            domain_id = domain['id']
            domain_name = domain['subdomain']
            
            # 拼接续期请求 URL
            renew_url = f"{full_url}&subdomain_id={domain_id}"
            renew_res = requests.get(renew_url, headers=headers, timeout=20).json()
            
            if renew_res.get("success"):
                results.append(f"✅ **{domain_name}**: 续期成功 (到期: {renew_res.get('new_expires_at')})")
            else:
                results.append(f"ℹ️ **{domain_name}**: {renew_res.get('message')}")

        report = "### DNSHE 自动续期任务完成\n" + "\n".join(results)
        print(report)
        send_pushplus(report)

    except Exception as e:
        error_msg = f"❌ 脚本运行异常: {str(e)}"
        print(error_msg)
        send_pushplus(error_msg)

if __name__ == "__main__":
    main()
