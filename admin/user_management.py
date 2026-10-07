from PyQt6.QtWidgets import QDialog, QFormLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QComboBox, QLineEdit, QVBoxLayout


class UserManagementDialog(QDialog):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.setWindowTitle("User Management")
        self.resize(460, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Managers can administer console accounts. Supervisors have no access to this window."))
        self.list = QListWidget()
        layout.addWidget(self.list)
        for label, handler in (("Add User", self.add_user), ("Edit User", self.edit_user), ("Delete User", self.delete_user)):
            button = QPushButton(label)
            button.clicked.connect(handler)
            layout.addWidget(button)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for username, record in sorted(self.store.users().items()):
            self.list.addItem(f"{username} ({record.get('role', 'supervisor')})")

    def _selected(self):
        item = self.list.currentItem()
        return item.text().split(" (", 1)[0] if item else None

    def _form(self, title, username="", role="supervisor", password_required=True):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        form = QFormLayout(dialog)
        username_input = QLineEdit(username)
        username_input.setReadOnly(bool(username))
        password_input = QLineEdit()
        password_input.setEchoMode(QLineEdit.EchoMode.Password)
        role_input = QComboBox()
        role_input.addItems(["manager", "supervisor"])
        role_input.setCurrentText(role)
        form.addRow("Username", username_input)
        form.addRow("Password", password_input)
        form.addRow("Role", role_input)
        submit = QPushButton("Save")
        submit.clicked.connect(dialog.accept)
        form.addRow(submit)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        password = password_input.text()
        if password_required and not password:
            QMessageBox.warning(self, "Invalid user", "A password is required.")
            return None
        return username_input.text(), password, role_input.currentText()

    def add_user(self):
        values = self._form("Add User")
        if values:
            try:
                self.store.add_user(*values)
                self.refresh()
            except ValueError as error:
                QMessageBox.warning(self, "Unable to add user", str(error))

    def edit_user(self):
        username = self._selected()
        if not username:
            return
        record = self.store.users()[username]
        values = self._form("Edit User", username, record.get("role", "supervisor"), False)
        if values:
            try:
                self.store.edit_user(username, values[1] or None, values[2])
                self.refresh()
            except ValueError as error:
                QMessageBox.warning(self, "Unable to edit user", str(error))

    def delete_user(self):
        username = self._selected()
        if not username:
            return
        if QMessageBox.question(self, "Delete user", f"Delete {username}?") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.delete_user(username)
            self.refresh()
        except ValueError as error:
            QMessageBox.warning(self, "Unable to delete user", str(error))