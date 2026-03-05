# Runner image — used during the run phase.
# Runs the full roboduck bug-finding loop with DinD for internal fuzzing.
FROM roboduck-base

COPY --from=libcrs . /libCRS
RUN /libCRS/install.sh \
    && /crs/.venv/bin/pip install /libCRS 2>/dev/null || true

ENTRYPOINT ["/opt/roboduck-oss-crs/run_roboduck.sh"]
