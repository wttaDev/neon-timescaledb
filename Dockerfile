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

#########################################################################################
#
# Layer "pg-build"
# Build Postgres from the neon postgres repository.
#
#########################################################################################
FROM build-deps AS pg-build
ARG PG_VERSION
COPY vendor/postgres-${PG_VERSION} postgres
RUN cd postgres && \
    export CONFIGURE_CMD="./configure CFLAGS='-O2 -g3' --enable-debug --with-openssl --with-uuid=ossp \
    --with-icu --with-libxml --with-libxslt --with-lz4" && \
    if [ "${PG_VERSION}" != "v14" ]; then \
        # zstd is available only from PG15
        export CONFIGURE_CMD="${CONFIGURE_CMD} --with-zstd"; \
    fi && \
    eval $CONFIGURE_CMD && \
    make MAKELEVEL=0 -j $(getconf _NPROCESSORS_ONLN) -s install && \
    make MAKELEVEL=0 -j $(getconf _NPROCESSORS_ONLN) -s -C contrib/ install && \
    # Install headers
    make MAKELEVEL=0 -j $(getconf _NPROCESSORS_ONLN) -s -C src/include install && \
    make MAKELEVEL=0 -j $(getconf _NPROCESSORS_ONLN) -s -C src/interfaces/libpq install && \
    # Enable some of contrib extensions
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/autoinc.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/bloom.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/earthdistance.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/insert_username.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/intagg.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/moddatetime.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pg_stat_statements.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pgrowlocks.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pgstattuple.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/refint.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/xml2.control
#########################################################################################
#
# Layer "postgis-build"
# Build PostGIS from the upstream PostGIS mirror.
#
#########################################################################################
FROM build-deps AS postgis-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/
RUN apt update && \
    apt install -y cmake gdal-bin libboost-dev libboost-thread-dev libboost-filesystem-dev \
    libboost-system-dev libboost-iostreams-dev libboost-program-options-dev libboost-timer-dev \
    libcgal-dev libgdal-dev libgmp-dev libmpfr-dev libopenscenegraph-dev libprotobuf-c-dev \
    protobuf-c-compiler xsltproc

# SFCGAL > 1.3 requires CGAL > 5.2, Bullseye's libcgal-dev is 5.2
RUN wget https://gitlab.com/Oslandia/SFCGAL/-/archive/v1.3.10/SFCGAL-v1.3.10.tar.gz -O SFCGAL.tar.gz && \
    echo "4e39b3b2adada6254a7bdba6d297bb28e1a9835a9f879b74f37e2dab70203232 SFCGAL.tar.gz" | sha256sum --check && \
    mkdir sfcgal-src && cd sfcgal-src && tar xvzf ../SFCGAL.tar.gz --strip-components=1 -C . && \
    cmake -DCMAKE_BUILD_TYPE=Release . && make -j $(getconf _NPROCESSORS_ONLN) && \
    DESTDIR=/sfcgal make install -j $(getconf _NPROCESSORS_ONLN) && \
    make clean && cp -R /sfcgal/* /

ENV PATH "/usr/local/pgsql/bin:$PATH"

RUN wget https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz -O postgis.tar.gz && \
    echo "9a2a219da005a1730a39d1959a1c7cec619b1efb009b65be80ffc25bad299068 postgis.tar.gz" | sha256sum --check && \
    mkdir postgis-src && cd postgis-src && tar xvzf ../postgis.tar.gz --strip-components=1 -C . && \
    ./autogen.sh && \
    ./configure --with-sfcgal=/usr/local/bin/sfcgal-config && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    cd extensions/postgis && \
    make clean && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/postgis.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/postgis_raster.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/postgis_sfcgal.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/postgis_tiger_geocoder.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/postgis_topology.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/address_standardizer.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/address_standardizer_data_us.control

RUN wget https://github.com/pgRouting/pgrouting/archive/v3.4.2.tar.gz -O pgrouting.tar.gz && \
    echo "cac297c07d34460887c4f3b522b35c470138760fe358e351ad1db4edb6ee306e pgrouting.tar.gz" | sha256sum --check && \
    mkdir pgrouting-src && cd pgrouting-src && tar xvzf ../pgrouting.tar.gz --strip-components=1 -C . && \
    mkdir build && \
    cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pgrouting.control

