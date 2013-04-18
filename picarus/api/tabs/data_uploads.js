function render_data_uploads(email_auth) {
    function success_user(xhr) {
        response = JSON.parse(xhr.responseText);

    }
    var AppView = Backbone.View.extend({
        el: $('#container'),
        initialize: function() {
            _.bindAll(this, 'render');
            this.model.bind('reset', this.render);
            this.model.bind('change', this.render);
        },
        render: function() {
            var startRow = _.unescape(this.model.pescape('upload_row_prefix'));
            var imageColumn = 'thum:image_150sq';
            function success(row, columns) {
                $('#images').append($('<img>').attr('src', 'data:image/jpeg;base64,' + base64.encode(columns[imageColumn])).attr('width', '150px'));
            }
            PICARUS.scanner("images", startRow, prefix_to_stop_row(startRow), {success: success, maxRows: 24, columns: [imageColumn]})
        }
    });
    var model = new PicarusUser({row: encode_id(email_auth.email)});
    new AppView({ model: model });
    model.fetch();
}