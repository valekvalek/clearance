FROM python:3.11-slim
RUN mkdir -p /etc && printf '# managed-by: vibe-platform (Mantis #253145)\n[global]\nindex-url = %s\ntimeout = 10\nretries = 2\n' 'https://mirrors.aliyun.com/pypi/simple/' > /etc/pip.conf
WORKDIR /opt/app
RUN ln -s /opt/app /app
COPY . .
RUN pip install -r requirements.txt
EXPOSE 8000
CMD ["/bin/sh","-c","python app.py"]

