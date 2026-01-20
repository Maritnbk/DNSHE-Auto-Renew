import requests
import os
import json

# 基于最新文档的配置 [cite: 1, 17]
API_BASE = "https://api005.dnshe.com/index.php?m=domain_hub"
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
    # 认证头部信息 [cite: 1, 18]
    headers = {
        "X-API-Key": API_KEY,
        "X-API-Secret": API_SECRET,
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    # 1. 获取子域名列表 [cite: 21, 22]
    list_url = f"{API_BASE}&endpoint=subdomains&action=list"

    try:
        print(f"正在获取域名列表...")
        response = requests.get(list_url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            err_msg = f"❌ API 请求失败，状态码: {response.status_code}"
            print(err_msg)
            send_pushplus(err_msg)
            return

        res_data = response.json()
        if not res_data.get("success"):
            send_pushplus(f"❌ 获取列表失败: {res_data.get('message', '未知错误')}")
            return
        
        subdomains = res_data.get("subdomains", [])
        if not subdomains:
            send_pushplus("ℹ️ 账号下暂无可用域名。")
            return

        results = []
        # 2. 遍历执行续期 (使用 POST 方法) 
        renew_url = f"{API_BASE}&endpoint=subdomains&action=renew"
        
        for domain in subdomains:
            domain_id = domain['id']
            domain_name = domain.get('full_domain') or domain.get('subdomain') [cite: 2]
            
            print(f"正在尝试续期: {domain_name} (ID: {domain_id})")
            
            # 准备 POST 数据 
            payload = {"subdomain_id": domain_id}
            
            try:
                # 执行 POST 续期请求 
                renew_res = requests.post(renew_url, headers=headers, json=payload, timeout=20).json()
                
                if renew_res.get("success"):
                    # 续期成功，记录新到期时间 
                    results.append(f"✅ **{domain_name}**: 成功 (新到期: {renew_res.get('new_expires_at')})")
                else:
                    # 记录未成功原因 
                    msg = renew_res.get('message', '未触发续期')
                    results.append(f"ℹ️ **{domain_name}**: {msg}")
            except Exception as e:
                results.append(f"❌ **{domain_name}**: 请求异常 ({str(e)})")

        # 3. 汇总发送报告
        report = "### DNSHE 自动续期任务完成\n" + "\n".join(results)
        print(report)
        send_pushplus(report)

    except Exception as e:
        error_msg = f"❌ 脚本运行异常: {str(e)}"
        print(error_msg)
        send_pushplus(error_msg)

if __name__ == "__main__":
    main()
