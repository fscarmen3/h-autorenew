"""
从 HUS_BATCH 环境变量读取账号信息，登录 Telegram 并生成 session string。

用法（单次交互式，同一进程内完成，无需分步）:
  HUS_BATCH='...' python3 setup_tg_session.py
然后按提示输入验证码（及两步验证密码）即可。

HUS_BATCH 格式（多个账号用分号分隔）:
  phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session
  tg_session 留空则自动生成并输出完整行
"""
import os, sys, re

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("[ERROR] 请先安装 telethon: pip3 install telethon")
    sys.exit(1)


def parse_batch():
    """解析 HUS_BATCH，返回第一个账号的字段"""
    raw = os.environ.get("HUS_BATCH", "").strip()
    if not raw:
        print("[ERROR] 缺少环境变量 HUS_BATCH")
        print("[INFO] 格式: phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session")
        sys.exit(1)

    # 取第一个非空行
    for line in re.split(r'[\n;]', raw):
        line = line.strip()
        if not line:
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 1:
            continue
        phone = parts[0]
        tg_api_id = int(parts[3]) if len(parts) > 3 and parts[3] else 0
        tg_api_hash = parts[4] if len(parts) > 4 and parts[4] else ""
        tg_session = parts[5] if len(parts) > 5 and parts[5] else ""
        return {
            "phone": phone,
            "tg_api_id": tg_api_id,
            "tg_api_hash": tg_api_hash,
            "tg_session": tg_session,
            "raw_parts": parts,
        }

    print("[ERROR] HUS_BATCH 中无有效账号")
    sys.exit(1)


def format_output_line(raw_parts, session_str):
    """将原始 parts 补齐到 6 位，第 6 位填入 session_str，输出完整行"""
    parts = list(raw_parts)
    while len(parts) < 6:
        parts.append("")
    parts[5] = session_str
    return ",".join(parts)


async def main():
    acc = parse_batch()
    phone = acc["phone"]
    api_id = acc["tg_api_id"]
    api_hash = acc["tg_api_hash"]
    tg_session = acc["tg_session"]

    if not api_id or not api_hash:
        print("[ERROR] HUS_BATCH 中缺少 tg_api_id 或 tg_api_hash（第4、5字段）")
        sys.exit(1)

    # 统一手机号格式为 +国码-号码
    phone = phone.strip()
    if not phone.startswith("+"):
        phone = "+" + phone

    print(f"[INFO] 手机号: {phone}")
    print(f"[INFO] API ID: {api_id}")

    # 如果已有 session，尝试直接连接验证
    if tg_session:
        print("[INFO] 检查已有 session...")
        try:
            client = TelegramClient(StringSession(tg_session), api_id, api_hash)
        except ValueError:
            print("[WARN] tg_session 非法（非有效 session 字符串），将改为创建新 session")
            tg_session = ""
        else:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"[INFO] ✅ Session 有效，已登录: {me.first_name} (@{me.username or 'N/A'})")
                await client.disconnect()
                # 输出原始行（session 不变）
                print(f"\n{'='*50}")
                print("[INFO] 当前 HUS_BATCH 行（session 有效，无需更新）:")
                print(f"{'='*50}")
                print(f"HUS_BATCH='{format_output_line(acc['raw_parts'], tg_session)}'")
                print(f"{'='*50}")
                return
            await client.disconnect()
            print("[INFO] Session 已失效，重新登录...")

    # 新建 session 登录（同一进程内发码 + 输入验证码，避免跨进程丢失 phone_code_hash）
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        # 理论上新建 session 不会已授权，保险处理
        session_str = client.session.save()
        _output_session(acc, session_str, client)
        return

    print("[INFO] 发送验证码...")
    sent = await client.send_code_request(phone)
    print("[INFO] 验证码已发送，请到 Telegram 查看")

    code = input("请输入验证码: ").strip()
    password = input("请输入两步验证密码（若无直接回车）: ").strip()
    try:
        await client.sign_in(
            phone, code, phone_code_hash=sent.phone_code_hash
        )
    except Exception as e:
        if "SessionPasswordNeededError" in str(type(e).__name__):
            if not password:
                print("[ERROR] 该账号需要两步验证密码，请重新运行并在提示处输入密码")
                await client.disconnect()
                sys.exit(1)
            await client.sign_in(password=password)
        else:
            raise

    await _output_session(acc, None, client)


async def _output_session(acc, session_str, client):
    if not session_str:
        session_str = client.session.save()
    me = await client.get_me()
    print(f"[INFO] ✅ Telegram 登录成功: {me.first_name} (@{me.username or 'N/A'}) phone={me.phone}")

    # 输出完整 HUS_BATCH 行
    output_line = format_output_line(acc["raw_parts"], session_str)
    print(f"\n{'='*50}")
    print("[INFO] 复制以下内容替换 HUS_BATCH 中对应的行（第6字段为 session）:")
    print(f"{'='*50}")
    print(f"HUS_BATCH='{output_line}'")
    print(f"{'='*50}")

    await client.disconnect()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
