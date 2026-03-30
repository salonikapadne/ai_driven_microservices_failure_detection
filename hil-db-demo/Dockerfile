FROM alpine:3.19
COPY demo-log.sh /demo-log.sh
RUN sed -i 's/\r$//' /demo-log.sh && chmod +x /demo-log.sh
ENV HIL_DEMO_INTERVAL_SEC=120
CMD ["/demo-log.sh"]
