import React, { useState } from 'react';
import {
  Card, Typography, Switch, Select, Divider, Space, Row, Col, InputNumber,
} from 'antd';

const { Title, Text } = Typography;

const Settings: React.FC = () => {
  const [pollInterval, setPollInterval] = useState(30);
  const [pageSize, setPageSize] = useState(20);
  const [language, setLanguage] = useState('zh-CN');
  const [compactView, setCompactView] = useState(false);

  return (
    <div>
      <Title level={3} style={{ margin: 0, fontWeight: 600, fontSize: 22, marginBottom: 24 }}>
        Settings
      </Title>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card title={<span style={{ fontSize: 16, fontWeight: 600 }}>⚙️ General</span>} style={{ borderRadius: 10, marginBottom: 16 }}>
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Auto-refresh Interval</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>Dashboard polling frequency (seconds)</Text>
                </div>
                <InputNumber
                  min={5}
                  max={300}
                  value={pollInterval}
                  onChange={(v) => setPollInterval(v || 30)}
                  style={{ width: 80 }}
                />
              </div>
              <Divider style={{ margin: '4px 0' }} />
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Page Size</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>Default items per page</Text>
                </div>
                <InputNumber
                  min={10}
                  max={100}
                  value={pageSize}
                  onChange={(v) => setPageSize(v || 20)}
                  style={{ width: 80 }}
                />
              </div>
              <Divider style={{ margin: '4px 0' }} />
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Language</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>UI language</Text>
                </div>
                <Select value={language} onChange={setLanguage} style={{ width: 120 }}>
                  <Select.Option value="zh-CN">中文</Select.Option>
                  <Select.Option value="en-US">English</Select.Option>
                </Select>
              </div>
              <Divider style={{ margin: '4px 0' }} />
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Compact View</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>Reduce spacing for denser display</Text>
                </div>
                <Switch checked={compactView} onChange={setCompactView} />
              </div>
            </Space>
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card title={<span style={{ fontSize: 16, fontWeight: 600 }}>🔗 API Configuration</span>} style={{ borderRadius: 10, marginBottom: 16 }}>
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Base URL</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>A2A Registry API endpoint</Text>
                </div>
                <code style={{ fontSize: 12, background: 'var(--bg)', padding: '4px 8px', borderRadius: 4 }}>
                  {window.location.origin}
                </code>
              </div>
              <Divider style={{ margin: '4px 0' }} />
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>Auth Enabled</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>OAuth 2.1 authentication</Text>
                </div>
                <Switch checked={!!localStorage.getItem('token')} />
              </div>
            </Space>
          </Card>

          <Card title={<span style={{ fontSize: 16, fontWeight: 600 }}>ℹ️ About</span>} style={{ borderRadius: 10 }}>
            <Space direction="vertical" size="small">
              <div><Text strong>A2A Registry</Text> — Management Console</div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                macOS-inspired admin interface for the Simple A2A Registry.
              </div>
              <div style={{ fontSize: 12, color: 'var(--text-secondary)' }}>
                Built with React + Ant Design · Design: macOS Apple Design Language
              </div>
            </Space>
          </Card>
        </Col>
      </Row>
    </div>
  );
};

export default Settings;