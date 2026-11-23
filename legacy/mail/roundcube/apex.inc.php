<?php
// Merged into Roundcube's config by the docker image. TLS to the mail server is verified
// against the Apex lab CA (legacy/mail/certs.py), never switched off.
$tls = ['ssl' => ['cafile' => '/certs/ca.crt', 'verify_peer' => true, 'verify_peer_name' => true]];
$config['imap_conn_options'] = $tls;
$config['smtp_conn_options'] = $tls;
$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';
$config['product_name'] = 'Apex Webmail';
$config['support_url'] = '';
