import asyncio
import os
import time
import httpx

# 1. 설정값 (포트 8100 적용)
BASE_URL = "http://127.0.0.1:8100"
LOGIN_URL = f"{BASE_URL}/api/auth/login"
SEND_URL = f"{BASE_URL}/api/fax/send"

DESTINATION_NUMBER = "01012345678"
FILE_PATH = "/tmp/20page_test.pdf"

USERNAME = "admin"
PASSWORD = "ajdajd89*$"


async def login(client: httpx.AsyncClient) -> bool:
    payload = {
        "username": USERNAME,
        "password": PASSWORD
    }
    try:
        response = await client.post(LOGIN_URL, json=payload, timeout=10.0)
        if response.status_code == 200:
            res_json = response.json()
            user_info = res_json.get("user", {})
            name = user_info.get("display_name") or user_info.get("username")
            role = user_info.get("role")
            print(f"🔑 로그인 성공: {name} ({role})")
            return True
        else:
            print(f"❌ 로그인 실패 ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"❌ 로그인 요청 예외: {e}")
        return False


async def send_single_fax(client: httpx.AsyncClient, index: int, file_bytes: bytes):
    data = {
        "number": DESTINATION_NUMBER,
        "to_name": f"수신자_{index:02d}",
        "title": f"30콜 동시 발송 #{index:02d}",
        "cover": "false"
    }
    files = {
        "file": ("20page_test.pdf", file_bytes, "application/pdf")
    }

    try:
        response = await client.post(
            SEND_URL,
            data=data,
            files=files,
            timeout=30.0
        )
        if response.status_code == 200:
            res_json = response.json()
            print(f"[콜 {index:02d}] ✅ 발송 요청 성공: job_id={res_json.get('job_id')} / status={res_json.get('status')}")
        else:
            print(f"[콜 {index:02d}] ❌ 실패 ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[콜 {index:02d}] ⚠️ 요청 예외: {e}")


async def main():
    if not os.path.exists(FILE_PATH):
        print(f"❌ 오류: 파일이 없습니다 -> {FILE_PATH}")
        return

    with open(FILE_PATH, "rb") as f:
        file_bytes = f.read()

    async with httpx.AsyncClient() as client:
        ok = await login(client)
        if not ok:
            print("로그인 실패로 테스트를 중단합니다.")
            return

        print(f"\n🚀 30콜 동시 발송 시작 (대상: {DESTINATION_NUMBER}, 파일: {FILE_PATH})")
        start_time = time.time()

        tasks = [send_single_fax(client, i + 1, file_bytes) for i in range(30)]
        await asyncio.gather(*tasks)

        print(f"\n🏁 30건 요청 완료 (총 소요 시간: {time.time() - start_time:.2f}초)")


if __name__ == "__main__":
    asyncio.run(main())
