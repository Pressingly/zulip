class zulip::mpass_sso {
  file { '/etc/nginx/zulip-include/app.d/mpass-sso.conf':
    ensure  => file,
    require => File['/etc/nginx/zulip-include/app.d'],
    owner   => 'root',
    group   => 'root',
    mode    => '0644',
    source  => 'puppet:///modules/zulip/nginx/zulip-include-app.d/mpass-sso.conf',
    notify  => Service['nginx'],
  }
}
