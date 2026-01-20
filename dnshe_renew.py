import requests
import os
import json

# 从环境变量获取配置
API_KEY = os.getenv('DNSHE_KEY')
API_SECRET = os.getenv('DNSHE_SECRET')
PUSHPLUS_TOKEN = os.getenv('PUSHPLUS_TOKEN')
PUSHPLUS_TOPIC = os.getenv('PUSHPLUS_TOPIC')

def send_pushplus(content):
    if not PUSHPLUS_TOKEN:
        print("未配置 PushPlus Token，跳过推送。")
        return
    
    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": "DNSHE 域名续期任务报告",
        "content": content,
        "template": "markdown",
        "topic": PUSHPLUS_TOPIC
    }
    requests.post(url, json=data)

def main():
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET
    }
    base_url = "https://api005.dnshe.com/index.php?m=domain_hub&endpoint=dns_records&action=list"

    # 1. 获取域名列表
    try:
        response = requests.get(base_url, headers=headers)
        res_data = response.json()
        if not res_data.get("success"):
            send_pushplus(f"获取域名列表失败: {res_data.get('message', '未知错误')}")
            return
        
        subdomains = res_data.get("subdomains", [])
        results = []

        # 2. 遍历域名执行续期
        for domain in subdomains:
            domain_id = domain['id']
            domain_name = domain['subdomain']
            
            # 续期请求
            renew_url = f"{base_url}&subdomain_id={domain_id}"
            renew_res = requests.get(renew_url, headers=headers).json()
            
            if renew_res.get("success"):
                results.append(f"✅ **{domain_name}**: 续期成功 (新到期: {renew_res.get('new_expires_at')})")
            else:
                # 即使失败也记录原因（例如未到180天窗口期）
                results.append(f"ℹ️ **{domain_name}**: {renew_res.get('message', '无需续期')}")

        # 3. 汇总发送报告
        report = "### DNSHE 自动续期任务完成\n" + "\n".join(results)
        print(report)
        send_pushplus(report)

    except Exception as e:
        error_msg = f"脚本运行出错: {str(e)}"
        print(error_msg)
        send_pushplus(error_msg)

if __name__ == "__main__":
    main()
