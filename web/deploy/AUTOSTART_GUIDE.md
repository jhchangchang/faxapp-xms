# FaxApp 자동 시작 설정 (systemd)

OS 재부팅 시 팩스 앱·팩스 서버가 자동으로 뜨고,
죽어도 자동 재시작되게 만듭니다. (Rocky Linux / systemd)

## 먼저 확인 (중요!)
서버 환경에 따라 경로가 다를 수 있으니 먼저 확인:

```bash
# 1. uvicorn 실제 경로
which uvicorn
# 예: /usr/local/bin/uvicorn 또는 /usr/bin/uvicorn

# 2. 앱 위치 확인 (main.py 있는 상위)
ls /opt/faxapp/web/app/main.py

# 3. PostgreSQL 서비스 이름 확인
systemctl list-units | grep -i postgres
# postgresql.service 또는 postgresql-15.service 등

# 4. FreeSWITCH 서비스 이름 확인 (팩스서버용)
systemctl list-units | grep -i freeswitch
```

## 설치 (자동 스크립트)
```bash
cd /opt/faxapp/web/deploy   # 서비스 파일 있는 곳
sudo bash install_services.sh
```
스크립트가 uvicorn 경로를 자동으로 찾아 반영하고 등록합니다.

## 설치 (수동)
자동 스크립트가 안 맞으면 수동으로:

```bash
# 1. 서비스 파일 복사
sudo cp faxapp.service /etc/systemd/system/
sudo cp faxserver.service /etc/systemd/system/

# 2. 경로 맞추기 (which uvicorn 결과로)
sudo nano /etc/systemd/system/faxapp.service
#   ExecStart= 줄의 uvicorn 경로를 실제 경로로 수정
#   PostgreSQL 서비스명이 다르면 After=/Wants= 도 수정

# 3. 등록
sudo systemctl daemon-reload
sudo systemctl enable faxapp      # 부팅 시 자동시작
sudo systemctl start faxapp       # 지금 시작
```

## 확인
```bash
# 상태 (active (running) 이어야 정상)
systemctl status faxapp
systemctl status faxserver

# 실시간 로그 보기
journalctl -u faxapp -f
#   "재시도 워커 시작" "Uvicorn running" 보이면 정상

# 재부팅 테스트
sudo reboot
# 재접속 후: systemctl status faxapp → 자동으로 running 이어야 함
```

## 운영 명령
```bash
systemctl start faxapp      # 시작
systemctl stop faxapp       # 정지
systemctl restart faxapp    # 재시작 (코드 업데이트 후)
systemctl status faxapp     # 상태
systemctl disable faxapp    # 자동시작 해제
journalctl -u faxapp -n 50  # 최근 로그 50줄
```

## 코드 업데이트 시
파일 교체 후:
```bash
cd /opt/faxapp/web
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
sudo systemctl restart faxapp
```
(이제 uvicorn 수동 실행 대신 systemctl restart 로)

## 팩스 서버(faxserver) 주의
faxserver.service는 faxserver_v6.py를 실행합니다.
- WorkingDirectory=/opt/faxapp 가 맞는지 확인 (faxserver_v6.py 위치)
- FreeSWITCH 서비스명이 freeswitch.service가 맞는지 확인
- 팩스 서버를 systemd로 안 돌리고 있었다면, 기존 실행 방식과 충돌 안 하게
  기존 프로세스 정리 후 등록

## 문제 해결
### status가 failed
```bash
journalctl -u faxapp -n 30   # 에러 원인 확인
```
흔한 원인:
- uvicorn 경로 틀림 → which uvicorn 으로 수정
- WorkingDirectory 틀림 → main.py 위치 확인
- DB 연결 실패 → PostgreSQL이 먼저 떠야 함 (After= 확인)

### "start request repeated too quickly"
→ 계속 죽어서 재시작 반복. journalctl로 실제 에러 확인.
   대개 경로나 임포트 문제.

### uvicorn을 못 찾음 (203/EXEC)
→ ExecStart의 uvicorn 절대경로가 틀림.
   which uvicorn 결과로 정확히 지정.
