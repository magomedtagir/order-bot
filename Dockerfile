FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ bot/
COPY data/ data/
COPY config.py main.py init_db.py import_data.py import_products.py ./
COPY *.xlsx ./

CMD ["python", "main.py"]
