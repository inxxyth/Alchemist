FROM php:8.3-apache
# No apt-get (Debian mirror is unreachable from this Docker daemon).
# mysqli/pdo_mysql compile from the PHP source tree already in the base image.
RUN docker-php-ext-install mysqli pdo_mysql \
    && a2enmod rewrite \
    && sed -i 's/AllowOverride None/AllowOverride All/g' /etc/apache2/apache2.conf
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
ENV COMPOSER_PROCESS_TIMEOUT=0
WORKDIR /var/www/html
EXPOSE 80
CMD ["apache2-foreground"]
