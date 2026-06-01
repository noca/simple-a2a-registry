import { Component, type ErrorInfo, type ReactNode } from 'react';
import { Card, Typography, Button, Space } from 'antd';

const { Title, Text } = Typography;

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error('[ErrorBoundary]', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div style={{
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          minHeight: 300,
          padding: 24,
        }}>
          <Card
            style={{
              maxWidth: 480,
              borderRadius: 10,
              textAlign: 'center',
            }}
          >
            <Space direction="vertical" size="middle">
              <span style={{ fontSize: 40 }}>⚠️</span>
              <Title level={4} style={{ margin: 0 }}>
                Page Crashed
              </Title>
              <Text type="secondary" style={{ fontSize: 13 }}>
                {this.state.error?.message || 'An unexpected error occurred while rendering this page.'}
              </Text>
              {import.meta.env.DEV && this.state.error && (
                <pre style={{
                  fontSize: 11,
                  background: 'var(--bg, #f5f5f5)',
                  padding: 12,
                  borderRadius: 8,
                  maxHeight: 150,
                  overflow: 'auto',
                  textAlign: 'left',
                  whiteSpace: 'pre-wrap',
                }}>
                  {this.state.error.stack}
                </pre>
              )}
              <Button type="primary" onClick={this.handleRetry}>
                Retry
              </Button>
            </Space>
          </Card>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;