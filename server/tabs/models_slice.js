function render_models_slice() {
    model_dropdown({modelFilter: function (x) {return true},
                    el: $('#model_select')});
    row_selector($('#rowPrefixDrop'), {startRow: $('#startRow'), stopRow: $('#stopRow')});
    $('#runButton').click(function () {
        button_running();
        var startRow = unescape($('#startRow').val());
        var stopRow = unescape($('#stopRow').val());
        var action = 'io/link';
        if ($('#chainCheck').is(':checked'))
            action = 'io/chain';
        var model = decode_id($('#model_select').find(":selected").val());
        PICARUS.postSlice('images', startRow, stopRow, {success:  _.partial(watchJob, {success: _.partial(updateJobStatus, $('#results')), done: button_reset}), fail: button_error, data: {action: action, model: model}});
    });
}