FROM python:3.14-alpine

WORKDIR /app
COPY requirements.txt main.py ./
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8003
CMD ["python", "main.py"]
