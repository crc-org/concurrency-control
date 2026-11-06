FROM registry.access.redhat.com/ubi9/ubi-minimal:latest

LABEL org.opencontainers.image.authors="CodeReady Containers <devtools-cdk@redhat.com>"

ENV TKN_URL="https://github.com/tektoncd/cli/releases/download/v0.42.0/tkn_0.42.0_Linux_x86_64.tar.gz"
ENV OC_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/latest/openshift-client-linux.tar.gz"

RUN microdnf install -y openssh-clients sshpass zip bash jq python3 pip tar \
    && curl -kLO ${OC_URL} \
    && tar xzvf openshift-client-linux.tar.gz -C /usr/local/bin \
    && rm -f openshift-client-linux.tar.gz \
    && curl -kL ${TKN_URL} -o tkn.tar.gz \
    && tar xvzf tkn.tar.gz -C /usr/local/bin \
    && rm -f tkn.tar.gz \
    && pip install --no-cache-dir paramiko flask pyyaml \
    && microdnf clean all \
    && chmod 777 -R /opt/

COPY *.py /opt/
COPY templates/ /opt/templates/

EXPOSE 8080

ENTRYPOINT ["python3", "/opt/control.py"]
