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
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"推送失败: {e}")

def main():
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    # 基础 API 地址
    base_url = "https://api005.dnshe.com/index.php?m=domain_hub&endpoint=dns_records&action=list"

    try:
        # 1. 获取域名列表
        print(f"正在请求域名列表...")
        response = requests.get(base_url, headers=headers, timeout=20)
        
        # 打印状态码和部分返回内容以便调试
        print(f"HTTP 状态码: {response.status_code}")
        if response.status_code != 200:
            error_log = f"API 请求失败，状态码: {response.status_code}\n返回内容: {response.text[:200]}"
            print(error_log)
            send_pushplus(error_log)
            return

        try:
            res_data = response.json()
        except json.JSONDecodeError:
            error_log = f"解析 JSON 失败。API 返回了非 JSON 内容:\n{response.text[:500]}"
            print(error_log)
            send_pushplus(error_log)
            return

        if not res_data.get("success"):
            send_pushplus(f"获取列表失败: {res_data.get('message', '未知内容')}")
            return
        
        subdomains = res_data.get("subdomains", [])
        results = []

        # 2. 遍历域名执行续期
        for domain in subdomains:
            domain_id = domain['id']
            domain_name = domain['subdomain']
            
            renew_url = f"{base_url}&subdomain_id={domain_id}"
            print(f"正在尝试续期: {domain_name} (ID: {domain_id})")
            
            renew_res_raw = requests.get(renew_url, headers=headers, timeout=20)
            try:
                renew_res = renew_res_raw.json()
                if renew_res.get("success"):
                    results.append(f"✅ **{domain_name}**: 续期成功 (到期: {renew_res.get('new_expires_at')})")
                else:
                    results.append(f"ℹ️ **{domain_name}**: {renew_res.get('message', '无需续期')}")
            except:
                results.append(f"❌ **{domain_name}**: 解析返回结果失败")

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
