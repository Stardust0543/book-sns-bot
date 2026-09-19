# 파이썬 및 Playwright 전용 공식 슬림 이미지 사용
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# 작업 디렉토리 설정
WORKDIR /app

# 필요한 파일 복사
COPY requirements.txt .

# 파이썬 패키지 설치
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 전체 소스 복사
COPY . .

# Render 실행 포트 노출
EXPOSE 10000

# 봇 실행
CMD ["python", "telegram_agent.py"]
