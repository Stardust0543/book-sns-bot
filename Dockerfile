# Playwright 공식 이미지: Chromium + 필요한 모든 시스템 라이브러리가 이미 설치되어 있음
# (libnss3, libatk 등 apt 의존성 문제를 원천적으로 피하는 가장 확실한 방법)
#
# ⚠️ 중요: 이 이미지 태그의 버전은 requirements.txt에 명시된 playwright의 pip 버전과
# 반드시 정확히 일치해야 합니다. 버전이 다르면 이미지 안의 크로미움 리비전과
# 파이썬 패키지가 기대하는 리비전이 어긋나서 "Executable doesn't exist" 에러가 납니다.
# requirements.txt에는 playwright==1.63.0 처럼 버전을 반드시 고정(pin)하세요.
# (고정하지 않으면 다음 배포 때 pip가 더 최신 버전을 설치하면서 이 문제가 재발합니다.)
FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render는 PORT 환경변수를 런타임에 주입합니다 (Flask 코드의 os.environ.get("PORT", 10000) 참고)
EXPOSE 10000

CMD ["python", "telegram_agent.py"]
