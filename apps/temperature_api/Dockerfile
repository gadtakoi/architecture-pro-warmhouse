FROM python:3.14-alpine

ENV PYTHONUNBUFFERED=1 \
    PORT=8081

WORKDIR /app

COPY main.py .

EXPOSE 8081

CMD ["python", "main.py"]
