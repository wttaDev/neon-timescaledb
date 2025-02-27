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

#########################################################################################
#
# Layer "pgtap-pg-build"
# compile pgTAP extension
#
#########################################################################################
FROM build-deps AS pgtap-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/theory/pgtap/archive/refs/tags/v1.2.0.tar.gz -O pgtap.tar.gz && \
    echo "9c7c3de67ea41638e14f06da5da57bac6f5bd03fea05c165a0ec862205a5c052 pgtap.tar.gz" | sha256sum --check && \
    mkdir pgtap-src && cd pgtap-src && tar xvzf ../pgtap.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pgtap.control

#########################################################################################
#
# Layer "ip4r-pg-build"
# compile ip4r extension
#
#########################################################################################
FROM build-deps AS ip4r-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/RhodiumToad/ip4r/archive/refs/tags/2.4.1.tar.gz -O ip4r.tar.gz && \
    echo "78b9f0c1ae45c22182768fe892a32d533c82281035e10914111400bf6301c726 ip4r.tar.gz" | sha256sum --check && \
    mkdir ip4r-src && cd ip4r-src && tar xvzf ../ip4r.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/ip4r.control

#########################################################################################
#
# Layer "prefix-pg-build"
# compile Prefix extension
#
#########################################################################################
FROM build-deps AS prefix-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/dimitri/prefix/archive/refs/tags/v1.2.9.tar.gz -O prefix.tar.gz && \
    echo "38d30a08d0241a8bbb8e1eb8f0152b385051665a8e621c8899e7c5068f8b511e prefix.tar.gz" | sha256sum --check && \
    mkdir prefix-src && cd prefix-src && tar xvzf ../prefix.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/prefix.control

#########################################################################################
#
# Layer "hll-pg-build"
# compile hll extension
#
#########################################################################################
FROM build-deps AS hll-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/citusdata/postgresql-hll/archive/refs/tags/v2.17.tar.gz -O hll.tar.gz && \
    echo "9a18288e884f197196b0d29b9f178ba595b0dfc21fbf7a8699380e77fa04c1e9 hll.tar.gz" | sha256sum --check && \
    mkdir hll-src && cd hll-src && tar xvzf ../hll.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/hll.control

#########################################################################################
#
# Layer "plpgsql-check-pg-build"
# compile plpgsql_check extension
#
#########################################################################################
FROM build-deps AS plpgsql-check-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN wget https://github.com/okbob/plpgsql_check/archive/refs/tags/v2.3.2.tar.gz -O plpgsql_check.tar.gz && \
    echo "9d81167c4bbeb74eebf7d60147b21961506161addc2aee537f95ad8efeae427b plpgsql_check.tar.gz" | sha256sum --check && \
    mkdir plpgsql_check-src && cd plpgsql_check-src && tar xvzf ../plpgsql_check.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config USE_PGXS=1 && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/plpgsql_check.control

#########################################################################################
#
# Layer "timescaledb-pg-build"
# compile timescaledb extension
#
#########################################################################################
FROM build-deps AS timescaledb-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ENV PATH "/usr/local/pgsql/bin:$PATH"

RUN apt-get update && \
    apt-get install -y cmake && \
    wget https://github.com/timescale/timescaledb/archive/refs/tags/2.10.1.tar.gz -O timescaledb.tar.gz && \
    echo "6fca72a6ed0f6d32d2b3523951ede73dc5f9b0077b38450a029a5f411fdb8c73 timescaledb.tar.gz" | sha256sum --check && \
    mkdir timescaledb-src && cd timescaledb-src && tar xvzf ../timescaledb.tar.gz --strip-components=1 -C . && \
    ./bootstrap -DSEND_TELEMETRY_DEFAULT:BOOL=OFF -DUSE_TELEMETRY:BOOL=OFF -DAPACHE_ONLY:BOOL=ON -DCMAKE_BUILD_TYPE=Release && \
    cd build && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make install -j $(getconf _NPROCESSORS_ONLN) && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/timescaledb.control

#########################################################################################
#
# Layer "pg-hint-plan-pg-build"
# compile pg_hint_plan extension
#
#########################################################################################
FROM build-deps AS pg-hint-plan-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ARG PG_VERSION
ENV PATH "/usr/local/pgsql/bin:$PATH"

RUN case "${PG_VERSION}" in \
      "v14") \
        export PG_HINT_PLAN_VERSION=14_1_4_1 \
        export PG_HINT_PLAN_CHECKSUM=c3501becf70ead27f70626bce80ea401ceac6a77e2083ee5f3ff1f1444ec1ad1 \
        ;; \
      "v15") \
        export PG_HINT_PLAN_VERSION=15_1_5_0 \
        export PG_HINT_PLAN_CHECKSUM=564cbbf4820973ffece63fbf76e3c0af62c4ab23543142c7caaa682bc48918be \
        ;; \
      *) \
        echo "Export the valid PG_HINT_PLAN_VERSION variable" && exit 1 \
        ;; \
    esac && \
    wget https://github.com/ossc-db/pg_hint_plan/archive/refs/tags/REL${PG_HINT_PLAN_VERSION}.tar.gz -O pg_hint_plan.tar.gz && \
    echo "${PG_HINT_PLAN_CHECKSUM} pg_hint_plan.tar.gz" | sha256sum --check && \
    mkdir pg_hint_plan-src && cd pg_hint_plan-src && tar xvzf ../pg_hint_plan.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make install -j $(getconf _NPROCESSORS_ONLN) && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/pg_hint_plan.control

#########################################################################################
#
# Layer "kq-imcx-pg-build"
# compile kq_imcx extension
#
#########################################################################################
FROM build-deps AS kq-imcx-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ENV PATH "/usr/local/pgsql/bin/:$PATH"
RUN apt-get update && \
    apt-get install -y git libgtk2.0-dev libpq-dev libpam-dev libxslt-dev libkrb5-dev cmake && \
    wget https://github.com/ketteq-neon/postgres-exts/archive/e0bd1a9d9313d7120c1b9c7bb15c48c0dede4c4e.tar.gz -O kq_imcx.tar.gz && \
    echo "dc93a97ff32d152d32737ba7e196d9687041cda15e58ab31344c2f2de8855336 kq_imcx.tar.gz" | sha256sum --check && \
    mkdir kq_imcx-src && cd kq_imcx-src && tar xvzf ../kq_imcx.tar.gz --strip-components=1 -C . && \
    mkdir build && \
    cd build && \
    cmake -DCMAKE_BUILD_TYPE=Release .. && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/kq_imcx.control

#########################################################################################
#
# Layer "pg-cron-pg-build"
# compile pg_cron extension
#
#########################################################################################
FROM build-deps AS pg-cron-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ENV PATH "/usr/local/pgsql/bin/:$PATH"
RUN wget https://github.com/citusdata/pg_cron/archive/refs/tags/v1.5.2.tar.gz -O pg_cron.tar.gz && \
    echo "6f7f0980c03f1e2a6a747060e67bf4a303ca2a50e941e2c19daeed2b44dec744 pg_cron.tar.gz" | sha256sum --check && \
    mkdir pg_cron-src && cd pg_cron-src && tar xvzf ../pg_cron.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pg_cron.control

#########################################################################################
#
# Layer "rdkit-pg-build"
# compile rdkit extension
#
#########################################################################################
FROM build-deps AS rdkit-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN apt-get update && \
    apt-get install -y \
        cmake \
        libboost-iostreams1.74-dev \
        libboost-regex1.74-dev \
        libboost-serialization1.74-dev \
        libboost-system1.74-dev \
        libeigen3-dev \
        libfreetype6-dev

ENV PATH "/usr/local/pgsql/bin/:/usr/local/pgsql/:$PATH"
RUN wget https://github.com/rdkit/rdkit/archive/refs/tags/Release_2023_03_1.tar.gz -O rdkit.tar.gz && \
    echo "db346afbd0ba52c843926a2a62f8a38c7b774ffab37eaf382d789a824f21996c rdkit.tar.gz" | sha256sum --check && \
    mkdir rdkit-src && cd rdkit-src && tar xvzf ../rdkit.tar.gz --strip-components=1 -C . && \
    cmake \
        -D RDK_BUILD_CAIRO_SUPPORT=OFF \
        -D RDK_BUILD_INCHI_SUPPORT=ON \
        -D RDK_BUILD_AVALON_SUPPORT=ON \
        -D RDK_BUILD_PYTHON_WRAPPERS=OFF \
        -D RDK_BUILD_DESCRIPTORS3D=OFF \
        -D RDK_BUILD_FREESASA_SUPPORT=OFF \
        -D RDK_BUILD_COORDGEN_SUPPORT=ON \
        -D RDK_BUILD_MOLINTERCHANGE_SUPPORT=OFF \
        -D RDK_BUILD_YAEHMOP_SUPPORT=OFF \
        -D RDK_BUILD_STRUCTCHECKER_SUPPORT=OFF \
        -D RDK_USE_URF=OFF \
        -D RDK_BUILD_PGSQL=ON \
        -D RDK_PGSQL_STATIC=ON \
        -D PostgreSQL_CONFIG=pg_config \
        -D PostgreSQL_INCLUDE_DIR=`pg_config --includedir` \
        -D PostgreSQL_TYPE_INCLUDE_DIR=`pg_config --includedir-server` \
        -D PostgreSQL_LIBRARY_DIR=`pg_config --libdir` \
        -D RDK_INSTALL_INTREE=OFF \
        -D CMAKE_BUILD_TYPE=Release \
        . && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/rdkit.control

#########################################################################################
#
# Layer "pg-uuidv7-pg-build"
# compile pg_uuidv7 extension
#
#########################################################################################
FROM build-deps AS pg-uuidv7-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ENV PATH "/usr/local/pgsql/bin/:$PATH"
RUN wget https://github.com/fboulnois/pg_uuidv7/archive/refs/tags/v1.0.1.tar.gz -O pg_uuidv7.tar.gz && \
    echo "0d0759ab01b7fb23851ecffb0bce27822e1868a4a5819bfd276101c716637a7a pg_uuidv7.tar.gz" | sha256sum --check && \
    mkdir pg_uuidv7-src && cd pg_uuidv7-src && tar xvzf ../pg_uuidv7.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/pg_uuidv7.control

#########################################################################################
#
# Layer "pg-roaringbitmap-pg-build"
# compile pg_roaringbitmap extension
#
#########################################################################################
FROM build-deps AS pg-roaringbitmap-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

ENV PATH "/usr/local/pgsql/bin/:$PATH"
RUN wget https://github.com/ChenHuajun/pg_roaringbitmap/archive/refs/tags/v0.5.4.tar.gz -O pg_roaringbitmap.tar.gz && \
    echo "b75201efcb1c2d1b014ec4ae6a22769cc7a224e6e406a587f5784a37b6b5a2aa pg_roaringbitmap.tar.gz" | sha256sum --check && \
    mkdir pg_roaringbitmap-src && cd pg_roaringbitmap-src && tar xvzf ../pg_roaringbitmap.tar.gz --strip-components=1 -C . && \
    make -j $(getconf _NPROCESSORS_ONLN) && \
    make -j $(getconf _NPROCESSORS_ONLN) install && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/roaringbitmap.control

#########################################################################################
#
# Layer "pg-anon-pg-build"
# compile anon extension
#
#########################################################################################
FROM build-deps AS pg-anon-pg-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

# Kaniko doesn't allow to do `${from#/usr/local/pgsql/}`, so we use `${from:17}` instead
ENV PATH "/usr/local/pgsql/bin/:$PATH"
RUN wget https://gitlab.com/dalibo/postgresql_anonymizer/-/archive/1.1.0/postgresql_anonymizer-1.1.0.tar.gz -O pg_anon.tar.gz && \
    echo "08b09d2ff9b962f96c60db7e6f8e79cf7253eb8772516998fc35ece08633d3ad pg_anon.tar.gz" | sha256sum --check && \
    mkdir pg_anon-src && cd pg_anon-src && tar xvzf ../pg_anon.tar.gz --strip-components=1 -C . && \
    find /usr/local/pgsql -type f | sort  > /before.txt && \
    make -j $(getconf _NPROCESSORS_ONLN) install PG_CONFIG=/usr/local/pgsql/bin/pg_config && \
    echo 'trusted = true' >> /usr/local/pgsql/share/extension/anon.control && \
    find /usr/local/pgsql -type f | sort  > /after.txt && \
    /bin/bash -c 'for from in $(comm -13 /before.txt /after.txt); do to=/extensions/anon/${from:17} && mkdir -p $(dirname ${to}) && cp -a ${from} ${to}; done'

#########################################################################################
#
# Layer "rust extensions"
# This layer is used to build `pgx` deps
#
#########################################################################################
FROM build-deps AS rust-extensions-build
COPY --from=pg-build /usr/local/pgsql/ /usr/local/pgsql/

RUN apt-get update && \
    apt-get install -y curl libclang-dev cmake && \
    useradd -ms /bin/bash nonroot -b /home

ENV HOME=/home/nonroot
ENV PATH="/home/nonroot/.cargo/bin:/usr/local/pgsql/bin/:$PATH"
USER nonroot
WORKDIR /home/nonroot
ARG PG_VERSION

RUN curl -sSO https://static.rust-lang.org/rustup/dist/$(uname -m)-unknown-linux-gnu/rustup-init && \
    chmod +x rustup-init && \
    ./rustup-init -y --no-modify-path --profile minimal --default-toolchain stable && \
    rm rustup-init && \
    cargo install --locked --version 0.7.3 cargo-pgx && \
    /bin/bash -c 'cargo pgx init --pg${PG_VERSION:1}=/usr/local/pgsql/bin/pg_config'

USER root

#########################################################################################
#
# Layer "pg-jsonschema-pg-build"
# Compile "pg_jsonschema" extension
#
#########################################################################################

FROM rust-extensions-build AS pg-jsonschema-pg-build

# caeab60d70b2fd3ae421ec66466a3abbb37b7ee6 made on 06/03/2023
# there is no release tag yet, but we need it due to the superuser fix in the control file, switch to git tag after release >= 0.1.5
RUN wget https://github.com/supabase/pg_jsonschema/archive/caeab60d70b2fd3ae421ec66466a3abbb37b7ee6.tar.gz -O pg_jsonschema.tar.gz && \
    echo "54129ce2e7ee7a585648dbb4cef6d73f795d94fe72f248ac01119992518469a4 pg_jsonschema.tar.gz" | sha256sum --check && \
    mkdir pg_jsonschema-src && cd pg_jsonschema-src && tar xvzf ../pg_jsonschema.tar.gz --strip-components=1 -C . && \
    sed -i 's/pgx = "0.7.1"/pgx = { version = "0.7.3", features = [ "unsafe-postgres" ] }/g' Cargo.toml && \
    cargo pgx install --release && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/pg_jsonschema.control

#########################################################################################
#
# Layer "pg-graphql-pg-build"
# Compile "pg_graphql" extension
#
#########################################################################################

FROM rust-extensions-build AS pg-graphql-pg-build

# b4988843647450a153439be367168ed09971af85 made on 22/02/2023 (from remove-pgx-contrib-spiext branch)
# Currently pgx version bump to >= 0.7.2  causes "call to unsafe function" compliation errors in
# pgx-contrib-spiext. There is a branch that removes that dependency, so use it. It is on the
# same 1.1 version we've used before.
RUN wget https://github.com/yrashk/pg_graphql/archive/b4988843647450a153439be367168ed09971af85.tar.gz -O pg_graphql.tar.gz && \
    echo "0c7b0e746441b2ec24187d0e03555faf935c2159e2839bddd14df6dafbc8c9bd pg_graphql.tar.gz" | sha256sum --check && \
    mkdir pg_graphql-src && cd pg_graphql-src && tar xvzf ../pg_graphql.tar.gz --strip-components=1 -C . && \
    sed -i 's/pgx = "~0.7.1"/pgx = { version = "0.7.3", features = [ "unsafe-postgres" ] }/g' Cargo.toml && \
    sed -i 's/pgx-tests = "~0.7.1"/pgx-tests = "0.7.3"/g' Cargo.toml && \
    cargo pgx install --release && \
    # it's needed to enable extension because it uses untrusted C language
    sed -i 's/superuser = false/superuser = true/g' /usr/local/pgsql/share/extension/pg_graphql.control && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/pg_graphql.control

#########################################################################################
#
# Layer "pg-tiktoken-build"
# Compile "pg_tiktoken" extension
#
#########################################################################################

FROM rust-extensions-build AS pg-tiktoken-pg-build

# 801f84f08c6881c8aa30f405fafbf00eec386a72 made on 10/03/2023
RUN wget https://github.com/kelvich/pg_tiktoken/archive/801f84f08c6881c8aa30f405fafbf00eec386a72.tar.gz -O pg_tiktoken.tar.gz && \
    echo "52f60ac800993a49aa8c609961842b611b6b1949717b69ce2ec9117117e16e4a pg_tiktoken.tar.gz" | sha256sum --check && \
    mkdir pg_tiktoken-src && cd pg_tiktoken-src && tar xvzf ../pg_tiktoken.tar.gz --strip-components=1 -C . && \
    cargo pgx install --release && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/pg_tiktoken.control

#########################################################################################
#
# Layer "pg-pgx-ulid-build"
# Compile "pgx_ulid" extension
#
#########################################################################################

FROM rust-extensions-build AS pg-pgx-ulid-build

RUN wget https://github.com/pksunkara/pgx_ulid/archive/refs/tags/v0.1.0.tar.gz -O pgx_ulid.tar.gz && \
    echo "908b7358e6f846e87db508ae5349fb56a88ee6305519074b12f3d5b0ff09f791 pgx_ulid.tar.gz" | sha256sum --check && \
    mkdir pgx_ulid-src && cd pgx_ulid-src && tar xvzf ../pgx_ulid.tar.gz --strip-components=1 -C . && \
    sed -i 's/pgx        = "=0.7.3"/pgx = { version = "0.7.3", features = [ "unsafe-postgres" ] }/g' Cargo.toml && \
    cargo pgx install --release && \
    echo "trusted = true" >> /usr/local/pgsql/share/extension/ulid.control

#########################################################################################
#
# Layer "neon-pg-ext-build"
# compile neon extensions
#
#########################################################################################
FROM build-deps AS neon-pg-ext-build
# Public extensions
COPY --from=postgis-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=postgis-build /sfcgal/* /
COPY --from=plv8-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=h3-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=h3-pg-build /h3/usr /
COPY --from=unit-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=vector-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pgjwt-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-jsonschema-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-graphql-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-tiktoken-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=hypopg-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-hashids-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=rum-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pgtap-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=ip4r-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=prefix-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=hll-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=plpgsql-check-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=timescaledb-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-hint-plan-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=kq-imcx-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-cron-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-pgx-ulid-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=rdkit-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-uuidv7-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY --from=pg-roaringbitmap-pg-build /usr/local/pgsql/ /usr/local/pgsql/
COPY pgxn/ pgxn/

RUN make -j $(getconf _NPROCESSORS_ONLN) \
        PG_CONFIG=/usr/local/pgsql/bin/pg_config \
        -C pgxn/neon \
        -s install && \
    make -j $(getconf _NPROCESSORS_ONLN) \
        PG_CONFIG=/usr/local/pgsql/bin/pg_config \
        -C pgxn/neon_utils \
        -s install && \
    make -j $(getconf _NPROCESSORS_ONLN) \
        PG_CONFIG=/usr/local/pgsql/bin/pg_config \
        -C pgxn/hnsw \
        -s install

#########################################################################################
#
# Compile and run the Neon-specific `compute_ctl` binary
#
#########################################################################################
FROM $REPOSITORY/$IMAGE:$TAG AS compute-tools
ARG BUILD_TAG
ENV BUILD_TAG=$BUILD_TAG

USER nonroot
# Copy entire project to get Cargo.* files with proper dependencies for the whole project
COPY --chown=nonroot . .

#########################################################################################
#
# Clean up postgres folder before inclusion
#
#########################################################################################
FROM neon-pg-ext-build AS postgres-cleanup-layer
COPY --from=neon-pg-ext-build /usr/local/pgsql /usr/local/pgsql

# Remove binaries from /bin/ that we won't use (or would manually copy & install otherwise)
RUN cd /usr/local/pgsql/bin && rm ecpg raster2pgsql shp2pgsql pgtopo_export pgtopo_import pgsql2shp

# Remove headers that we won't need anymore - we've completed installation of all extensions
RUN rm -r /usr/local/pgsql/include

# Remove static postgresql libraries - all compilation is finished, so we
# can now remove these files - they must be included in other binaries by now
# if they were to be used by other libraries.
RUN rm /usr/local/pgsql/lib/lib*.a

#########################################################################################
#
# Extenstion only
#
#########################################################################################
FROM neon-pg-ext-build AS postgres-cleanup-layer
# After the transition this layer will include all extensitons.
# As for now, it's only for new custom ones
#
# # Default extensions
# COPY --from=postgres-cleanup-layer /usr/local/pgsql/share/extension /usr/local/pgsql/share/extension
# COPY --from=postgres-cleanup-layer /usr/local/pgsql/lib             /usr/local/pgsql/lib
# Custom extensions
COPY --from=pg-anon-pg-build /extensions/anon/lib/ /extensions/anon/lib
COPY --from=pg-anon-pg-build /extensions/anon/share/extension /extensions/anon/share/extension

#########################################################################################
#
# Final layer
# Put it all together into the final image
#
#########################################################################################
FROM debian:bullseye-slim
# Add user postgres
RUN mkdir /var/db && useradd -m -d /var/db/postgres postgres && \
    echo "postgres:test_console_pass" | chpasswd && \
    mkdir /var/db/postgres/compute && mkdir /var/db/postgres/specs && \
    chown -R postgres:postgres /var/db/postgres && \
    chmod 0750 /var/db/postgres/compute && \
    echo '/usr/local/lib' >> /etc/ld.so.conf && /sbin/ldconfig && \
    # create folder for file cache
    mkdir -p -m 777 /neon/cache

COPY --from=postgres-cleanup-layer --chown=postgres /usr/local/pgsql /usr/local
COPY --from=compute-tools --chown=postgres /home/nonroot/target/release-line-debug-size-lto/compute_ctl /usr/local/bin/compute_ctl

# # Install:
# # libreadline8 for psql
# # libicu67, locales for collations (including ICU and plpgsql_check)
# # liblz4-1 for lz4
# # libossp-uuid16 for extension ossp-uuid
# # libgeos, libgdal, libsfcgal1, libproj and libprotobuf-c1 for PostGIS
# # libxml2, libxslt1.1 for xml2
# # libzstd1 for zstd
# # libboost*, libfreetype6, and zlib1g for rdkit
# RUN apt update &&  \
#     apt install --no-install-recommends -y \
#         gdb \
#         libicu67 \
#         liblz4-1 \
#         libreadline8 \
#         libboost-iostreams1.74.0 \
#         libboost-regex1.74.0 \
#         libboost-serialization1.74.0 \
#         libboost-system1.74.0 \
#         libossp-uuid16 \
#         libfreetype6 \
#         libgeos-c1v5 \
#         libgdal28 \
#         libproj19 \
#         libprotobuf-c1 \
#         libsfcgal1 \
#         libxml2 \
#         libxslt1.1 \
#         libzstd1 \
#         libcurl4-openssl-dev \
#         locales \
#         procps \
#         zlib1g && \
#     rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* && \
#     localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8

# ENV LANG en_US.utf8
# USER postgres
# ENTRYPOINT ["/usr/local/bin/compute_ctl"]