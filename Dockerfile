FROM python:3.13-slim

WORKDIR /app

# 必要なシステムパッケージとpip依存関係のインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションコードのコピー
COPY . .

# 永続ボリューム用ディレクトリの作成
RUN mkdir -p data

# 実行
CMD ["python", "bot.py"]
