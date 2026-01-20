import requests
import os
import json

# 直接硬编码 API 基础地址
API_BASE = "https://api005.dnshe.com/index.php?m=domain_hub"

# 从 Secrets 获取密钥和推送配置
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
        "title": "DNSHE 域名续期报告",
        "content": content,
        "template": "markdown",
        "topic": PUSHPLUS_TOPIC
    }
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
    
    # 拼接获取列表的完整参数
    list_url = f"{API_BASE}&endpoint=subdomains&action=list"

    try:
        print(f"正在请求列表: {list_url}")
        response = requests.get(list_url, headers=headers, timeout=20)
        
        # 调试输出
        if response.status_code != 200:
            err_msg = f"❌ API 访问失败\n状态码: {response.status_code}\n地址: {list_url}"
            print(err_msg)
            send_pushplus(err_msg)
            return

        res_data = response.json()
        if not res_data.get("success"):
            send_pushplus(f"❌ 获取列表失败: {res_data.get('message', '未知错误')}")
            return
        
        subdomains = res_data.get("subdomains", [])
        results = []

        # 遍历域名并执行续期
        for domain in subdomains:
            domain_id = domain['id']
            domain_name = domain['subdomain']
            
            # 续期命令通过在列表 URL 后追加 subdomain_id 实现
            renew_url = f"{list_url}&subdomain_id={domain_id}"
            print(f"尝试续期: {domain_name} (ID: {domain_id})")
            
            try:
                renew_res = requests.get(renew_url, headers=headers, timeout=20).json()
                if renew_res.get("success"):
                    # 记录新到期时间
                    results.append(f"✅ **{domain_name}**: 成功 (到期: {renew_res.get('new_expires_at')})")
                else:
                    # 记录未成功原因（例如：距离到期还超过 180 天）
                    results.append(f"ℹ️ **{domain_name}**: {renew_res.get('message', '未触发续期')}")
            except:
                results.append(f"❌ **{domain_name}**: 解析返回数据失败")

        report = "### DNSHE 自动续期任务完成\n" + "\n".join(results)
        print(report)
        send_pushplus(report)

    except Exception as e:
        error_msg = f"❌ 脚本运行异常: {str(e)}"
        print(error_msg)
        send_pushplus(error_msg)

if __name__ == "__main__":
    main()
