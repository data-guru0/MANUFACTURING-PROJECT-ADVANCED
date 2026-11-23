# Apex's file server: \\fileserver\Quotes. Plain Samba on Debian, configured the way a small shop does it.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends samba \
 && rm -rf /var/lib/apt/lists/*
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 445
ENTRYPOINT ["/entrypoint.sh"]
