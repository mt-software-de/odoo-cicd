{% include "static/tools.js" %}

// start live valuen
var current_details = null;
setTimeout(update_live_values, 0);
setTimeout(update_resources, 0);


{% include "static/index_functions.js" %}
{% include "static/index_backupdb_form.js" %}
{% include "static/index_delete_instance_form.js" %}
{% include "static/index_make_new_instance_form.js" %}
{% include "static/index_appsettings.js" %}
{% include "static/index_reset_form.js" %}
{% include "static/index_settings_form.js" %}
{% include "static/index_ui.js" %}