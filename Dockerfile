ARG PG_VERSION=v14
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


#########################################################################################
#
# Layer "plv8-build"
# Build plv8
#
#########################################################################################
FROM build-deps AS plv8-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/
RUN apt update && \
    apt install -y ninja-build python3-dev libncurses5 binutils clang

RUN wget https://github.com/plv8/plv8/archive/refs/tags/v3.1.5.tar.gz -O plv8.tar.gz && \
    echo "1e108d5df639e4c189e1c5bdfa2432a521c126ca89e7e5a969d46899ca7bf106 plv8.tar.gz" | sha256sum --check && \
    mkdir plv8-src && cd plv8-src && tar xvzf ../plv8.tar.gz --strip-components=1 -C . && \
    export PATH="/usr/local/pgsql/bin:$PATH" && \
    make DOCKER=1 -j $(getconf _NPROCESSORS_ONLN) install && \
    rm -rf /plv8-* && \
    find /usr/local/pgsql/ -name "plv8-*.so" | xargs strip && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/plv8.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/plcoffee.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/plls.control

#########################################################################################
#
# Layer "h3-pg-build"
# Build h3_pg
#
#########################################################################################
FROM build-deps AS h3-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

# packaged cmake is too old
RUN wget https://github.com/Kitware/CMake/releases/download/v3.24.2/cmake-3.24.2-linux-x86_64.sh \
      -q -O /tmp/cmake-install.sh \
      && echo "739d372726cb23129d57a539ce1432453448816e345e1545f6127296926b6754 /tmp/cmake-install.sh" | sha256sum --check \
      && chmod u+x /tmp/cmake-install.sh \
      && /tmp/cmake-install.sh --skip-license --prefix=/usr/local/ \
      && rm /tmp/cmake-install.sh

RUN wget https://github.com/uber/h3/archive/refs/tags/v4.1.0.tar.gz -O h3.tar.gz && \
    echo "ec99f1f5974846bde64f4513cf8d2ea1b8d172d2218ab41803bf6a63532272bc h3.tar.gz" | sha256sum --check && \
    mkdir h3-src && cd h3-src && tar xvzf ../h3.tar.gz --strip-components=1 -C . && \
    mkdir build && cd build && \
    cmake .. -DCMAKE_BUILD_TYPE=Release && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    DESTDIR=/h3 make install && \
    cp -R /h3/usr / && \
    rm -rf build

RUN wget https://github.com/zachasme/h3-pg/archive/refs/tags/v4.1.2.tar.gz -O h3-pg.tar.gz && \
    echo "c135aa45999b2ad1326d2537c1cadef96d52660838e4ca371706c08fdea1a956 h3-pg.tar.gz" | sha256sum --check && \
    mkdir h3-pg-src && cd h3-pg-src && tar xvzf ../h3-pg.tar.gz --strip-components=1 -C . && \
    export PATH="/usr/local/pgsql/bin:$PATH" && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/h3.control && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/h3_postgis.control

#########################################################################################
#
# Layer "unit-pg-build"
# compile unit extension
#
#########################################################################################
FROM build-deps AS unit-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/df7cb/postgresql-unit/archive/refs/tags/7.7.tar.gz -O postgresql-unit.tar.gz && \
    echo "411d05beeb97e5a4abf17572bfcfbb5a68d98d1018918feff995f6ee3bb03e79 postgresql-unit.tar.gz" | sha256sum --check && \
    mkdir postgresql-unit-src && cd postgresql-unit-src && tar xvzf ../postgresql-unit.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    # unit extension's "create extension" script relies on absolute install path to fill some reference tables.
    # We move the extension from '/usr/local/pgsql/' to '/usr/local/'  after it is build. So we need to adjust the path.
    # This one-liner removes pgsql/ part of the path.
    # NOTE: Other extensions that rely on MODULEDIR variable after building phase will need the same fix.
    find /usr/local/pgsql/share/extension/ -name "unit*.sql" -print0 | xargs -0 sed -i "s|pgsql/||g" && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/unit.control

#########################################################################################
#
# Layer "vector-pg-build"
# compile pgvector extension
#
#########################################################################################
FROM build-deps AS vector-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/pgvector/pgvector/archive/refs/tags/v0.4.0.tar.gz -O pgvector.tar.gz && \
    echo "b76cf84ddad452cc880a6c8c661d137ddd8679c000a16332f4f03ecf6e10bcc8 pgvector.tar.gz" | sha256sum --check && \
    mkdir pgvector-src && cd pgvector-src && tar xvzf ../pgvector.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/vector.control

#########################################################################################
#
# Layer "pgjwt-pg-build"
# compile pgjwt extension
#
#########################################################################################
FROM build-deps AS pgjwt-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

# 9742dab1b2f297ad3811120db7b21451bca2d3c9 made on 13/11/2021
RUN wget https://github.com/michelp/pgjwt/archive/9742dab1b2f297ad3811120db7b21451bca2d3c9.tar.gz -O pgjwt.tar.gz && \
    echo "cfdefb15007286f67d3d45510f04a6a7a495004be5b3aecb12cda667e774203f pgjwt.tar.gz" | sha256sum --check && \
    mkdir pgjwt-src && cd pgjwt-src && tar xvzf ../pgjwt.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pgjwt.control

#########################################################################################
#
# Layer "hypopg-pg-build"
# compile hypopg extension
#
#########################################################################################
FROM build-deps AS hypopg-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/HypoPG/hypopg/archive/refs/tags/1.3.1.tar.gz -O hypopg.tar.gz && \
    echo "e7f01ee0259dc1713f318a108f987663d60f3041948c2ada57a94b469565ca8e hypopg.tar.gz" | sha256sum --check && \
    mkdir hypopg-src && cd hypopg-src && tar xvzf ../hypopg.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/hypopg.control

#########################################################################################
#
# Layer "pg-hashids-pg-build"
# compile pg_hashids extension
#
#########################################################################################
FROM build-deps AS pg-hashids-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/iCyberon/pg_hashids/archive/refs/tags/v1.2.1.tar.gz -O pg_hashids.tar.gz && \
    echo "74576b992d9277c92196dd8d816baa2cc2d8046fe102f3dcd7f3c3febed6822a pg_hashids.tar.gz" | sha256sum --check && \
    mkdir pg_hashids-src && cd pg_hashids-src && tar xvzf ../pg_hashids.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pg_hashids.control

#########################################################################################
#
# Layer "rum-pg-build"
# compile rum extension
#
#########################################################################################
FROM build-deps AS rum-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/postgrespro/rum/archive/refs/tags/1.3.13.tar.gz -O rum.tar.gz && \
    echo "6ab370532c965568df6210bd844ac6ba649f53055e48243525b0b7e5c4d69a7d rum.tar.gz" | sha256sum --check && \
    mkdir rum-src && cd rum-src && tar xvzf ../rum.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/rum.control

