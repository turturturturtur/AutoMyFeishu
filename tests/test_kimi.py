import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
api_key = os.environ.get("KIMI_API_KEY")

try:
    client = OpenAI(
        api_key=api_key,
        base_url="https://api.kimi.com/coding/v1",
        # 【核心破解魔法】伪装 HTTP 请求头，告诉 Kimi 服务器：“我是 Claude Code”
        default_headers={"User-Agent": "claude-code/0.1.0"}
    )
    response = client.chat.completions.create(
        model="k2.6-code-preview",
        messages=[{"role": "user", "content": "你好，测试一下"}]
    )
    print("✅ 测试成功！Kimi 返回：", response.choices[0].message.content)
except Exception as e:
    print(f"❌ 测试失败！报错详情：{e}")
