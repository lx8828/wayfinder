"""Web 服务器 - 支持局域网 + ngrok 公网穿透
"""
import os
import sys
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from openai import OpenAI

from first_agent.agent import Agent
from first_agent.main import TravelAssistant
from first_agent.tools.base import get_tool_schemas
from first_agent.tools.registry import tool_registry

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")

# 全局 Agent 实例
_client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)
_tools_schemas = get_tool_schemas()
_agent = TravelAssistant(_client, _tools_schemas, tool_registry)


@app.route("/")
def index():
    """返回前端页面"""
    return send_from_directory("templates", "index.html")


@app.route("/api/chat", methods=["POST"])
def chat():
    """聊天接口"""
    data = request.get_json()
    if not data:
        return jsonify({"error": "请求体为空"}), 400
    
    message = data.get("message", "").strip()
    mode = data.get("mode", "1")
    
    if not message:
        return jsonify({"error": "消息为空"}), 400
    
    try:
        answer = _agent.chat(message, mode)
        return jsonify({"answer": answer, "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/memory", methods=["GET"])
def get_memory():
    """获取当前所有记忆"""
    from first_agent.memory import get_all_memory
    memories = get_all_memory()
    return jsonify([{"key": k, "value": v} for k, v in memories])


@app.route("/api/memory", methods=["DELETE"])
def clear_memory():
    """清空所有记忆"""
    from first_agent.memory import clear_memory as cm
    cm()
    return jsonify({"status": "ok"})


def main():
    """启动 Web 服务器，支持 --ngrok 参数开启公网穿透"""
    use_ngrok = "--ngrok" in sys.argv
    
    print("=" * 50)
    print("  智能出行助手 - Web 版")
    print("  本地访问: http://localhost:5000")
    print("=" * 50)
    
    if use_ngrok:
        print("  正在启动 ngrok 穿透...")
        try:
            from pyngrok import ngrok
            
            # 从 .env 读取 token 并设置
            authtoken = os.getenv("NGROK_AUTHTOKEN")
            if authtoken:
                ngrok.set_auth_token(authtoken)
                print(f"  ✅ 已配置 ngrok authtoken")
            else:
                print("  ⚠️  .env 中未找到 NGROK_AUTHTOKEN")
                print("  请到 https://dashboard.ngrok.com/get-started/your-authtoken 复制 token")
                print("  然后在 .env 中添加: NGROK_AUTHTOKEN=你的token")
                print()
            
            tunnel = ngrok.connect(5000)
            
            print(f"\n  ✅ ngrok 公网地址: {tunnel.public_url}")
            print(f"  把这个地址发给室友即可访问！")
            print("=" * 50)
            
            try:
                app.run(host="0.0.0.0", port=5000, debug=False)
            finally:
                ngrok.disconnect(tunnel.public_url)
                ngrok.kill()
        except ImportError:
            print("  ❌ pyngrok 未安装，运行: uv add pyngrok")
            app.run(host="0.0.0.0", port=5000, debug=False)
        except Exception as e:
            print(f"  ❌ ngrok 启动失败: {e}")
            print("  回退到局域网模式...")
            app.run(host="0.0.0.0", port=5000, debug=False)
    else:
        print("  局域网访问: http://你的IP:5000")
        print("  公网穿透: python web.py --ngrok")
        print("=" * 50)
        app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()