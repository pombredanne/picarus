function render_annotate_class() {
    row_selector($('#rowPrefixDrop'), {startRow: $('#startRow'), stopRow: $('#stopRow')});
    $('#runButton').click(function () {
        var startRow = $('#startRow').val();
        var stopRow = $('#stopRow').val();
        var imageColumn = base64.encode('thum:image_150sq');
        var numTasks = Number($('#num_tasks').val());
        var classColumn = base64.encode($('#class').val());
        function success(response) {
            ANNOTATIONS.fetch();
            $('#results').append($('<a>').attr('href', '/a1/annotate/' + response.task + '/index.html').text('Worker').attr('target', '_blank'));
        }
        PICARUS.postSlice('images', startRow, stopRow, {success: success, data: {action: 'io/annotate/image/class', imageColumn: imageColumn, classColumn: classColumn, instructions: $('#instructions').val(), numTasks: numTasks, mode: "amt"}});
    });
}