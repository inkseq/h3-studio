"""Offline Inky mount checks; trusted host adoption remains deferred."""

from tools.check_migration_deploy import require, secret_names


def validate_inky(document):
    """Advice credentials are opt-in, fixed-path, and API-only."""
    services, secrets = document['services'], document.get('secrets', {})
    app = services['app']
    selected = app.get('environment', {}).get('INKSEQ_INKY_CONFIG_FILE', '')
    require(selected in {'', '/run/secrets/inky'}, 'migration_inky_configuration_invalid')
    for name, service in services.items():
        require(name == 'app' or ('INKSEQ_INKY_CONFIG_FILE' not in service.get('environment', {})
                and 'inky' not in secret_names(service)), 'migration_inky_outside_app')
    configured = secrets.get('inky')
    if configured is None:
        require(not selected and 'inky' not in secret_names(app), 'migration_inky_configuration_invalid')
        return  # Existing rollback bundles need no new file.
    expected = '/run/sixnine-secrets/inky' if selected else secrets['execution_profiles']['file']
    require(configured.get('file') == expected and 'inky' in secret_names(app),
            'migration_inky_configuration_invalid')
    for item in app.get('secrets', []):
        if isinstance(item, dict) and item.get('source') == 'inky':
            require(item.get('target', 'inky') in {'inky', '/run/secrets/inky'},
                    'migration_inky_configuration_invalid')


