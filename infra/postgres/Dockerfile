FROM postgres:16

COPY configure_database_roles.sh /usr/local/bin/configure-database-roles

RUN chmod 0555 /usr/local/bin/configure-database-roles

ENTRYPOINT ["/usr/local/bin/configure-database-roles"]
