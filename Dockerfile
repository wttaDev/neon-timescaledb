ARG PG_VERSION=14
ARG REPOSITORY=neondatabase
ARG IMAGE=rust
ARG TAG=pinned
ARG BUILD_TAG

#########################################################################################
#
# Layer "build-deps"
#
#########################################################################################
FROM debian:bullseye-slim AS build-deps
RUN apt update &&  \
    apt install -y git autoconf automake libtool build-essential bison flex libreadline-dev \
    zlib1g-dev libxml2-dev libcurl4-openssl-dev libossp-uuid-dev wget pkg-config libssl-dev \
    libicu-dev libxslt1-dev liblz4-dev libzstd-dev
